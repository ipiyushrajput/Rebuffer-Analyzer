"""Segment sampler.

Every new segment on the lowest, middle and highest video rung is downloaded, plus every
segment of every audio rendition. Other rungs are sampled every Nth segment. Each download
is measured (bytes, TTFB, total time, throughput) and parsed, and the measurement is what the
Virtual Player Buffer replays.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from app.drm.context import DrmContext
from app.media.fmp4 import Fmp4Analysis, parse_init
from app.media.segment import SegmentAnalysis, analyse
from app.net.fetcher import Fetcher, FetchResult

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SampledSegment:
    """One measured segment fetch with its parsed contents."""

    variant: str
    msn: int
    uri: str
    requested_at: dt.datetime
    completed_at: dt.datetime
    result: FetchResult
    analysis: SegmentAnalysis
    declared_duration: float | None
    discontinuity_before: bool = False
    # A protected segment after decryption, with its initialisation segment in front of it, so
    # a decoder reads a whole file rather than a fragment. Empty for a clear segment, which is
    # already what the decoder reads.
    decoded_body: bytes = b""
    # Why a protected segment was not decrypted. Empty when it was, or when it was never
    # encrypted in the first place.
    drm_reason: str = ""

    @property
    def available(self) -> bool:
        return self.result.ok and self.result.bytes_received > 0

    @property
    def decodable_body(self) -> bytes:
        """The bytes a decoder reads: the decrypted payload when the segment was protected."""
        return self.decoded_body or self.result.body

    @property
    def download_ms(self) -> float:
        return self.result.timings.total_ms

    @property
    def measured_kbps(self) -> float | None:
        bitrate = self.analysis.measured_bitrate_bps
        return bitrate / 1000 if bitrate else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "msn": self.msn,
            "uri": self.uri,
            "at": self.completed_at.isoformat(),
            "status": self.result.status,
            "bytes": self.result.bytes_received,
            "download_ms": self.download_ms,
            "ttfb_ms": self.result.timings.ttfb_ms,
            "declared_duration": self.declared_duration,
            "actual_duration": self.analysis.actual_duration,
            "measured_kbps": self.measured_kbps,
            "av_skew_ms": self.analysis.av_skew_ms,
            "container": self.analysis.container,
            "starts_with_keyframe": self.analysis.starts_with_keyframe,
        }


@dataclass(slots=True)
class DecryptedBody:
    """What came out of the decrypt step, and what the parsers are to make of it."""

    body: bytes
    encrypted: bool
    # Whether a decrypt actually ran, as opposed to the body having been clear all along.
    ran: bool = False
    reason: str = ""


@dataclass
class RungSampling:
    """Sampling policy and state for one rung."""

    variant: str
    full: bool
    nth: int = 3
    fetched_msns: set[int] = field(default_factory=set)
    init_segment: Fmp4Analysis | None = None
    init_uri: str | None = None
    # The `EXT-X-MAP` body itself, kept because a decoder handed a bare `moof`/`mdat` fragment
    # has no `moov` to read the track from. Only a protected rung keeps it.
    init_body: bytes = b""

    def should_fetch(self, msn: int) -> bool:
        if msn in self.fetched_msns:
            return False
        if self.full:
            return True
        return msn % self.nth == 0


def choose_full_rungs(variant_ids: list[str]) -> set[str]:
    """Lowest, middle and highest rung are sampled in full; the rest every Nth segment."""
    if len(variant_ids) <= 3:
        return set(variant_ids)
    return {variant_ids[0], variant_ids[len(variant_ids) // 2], variant_ids[-1]}


class SegmentSampler:
    """Downloads and parses segments for one rung."""

    def __init__(
        self,
        sampling: RungSampling,
        fetcher: Fetcher,
        *,
        encrypted: bool = False,
        drm: DrmContext | None = None,
        max_tracked_msns: int = 2000,
    ) -> None:
        self.sampling = sampling
        self.fetcher = fetcher
        self.encrypted = encrypted
        self.drm = drm
        self.max_tracked_msns = max_tracked_msns

    async def ensure_init(self, init_uri: str | None) -> FetchResult | None:
        """Fetch and parse the EXT-X-MAP target once per rung.

        On a protected ladder this is also where the rung's protection is read and its content
        key obtained: `tenc` declares the scheme, the initialisation-vector size and the key
        identifier of this track, and none of the three is knowable from the playlist alone.
        """
        if not init_uri or self.sampling.init_uri == init_uri:
            return None
        result = await self.fetcher.fetch(init_uri)
        # Recorded before the parse, so an init segment the analyzer cannot read is fetched
        # once rather than on every poll.
        self.sampling.init_uri = init_uri
        if result.ok:
            try:
                self.sampling.init_segment = parse_init(result.body)
            except Exception:
                log.exception(
                    "EXT-X-MAP parse failed for %s (%s bytes from %s)",
                    self.sampling.variant,
                    result.bytes_received,
                    init_uri,
                )
            if self.encrypted and self.drm is not None:
                self.sampling.init_body = result.body
                await self.drm.read_init(self.sampling.variant, result.body)
        return result

    async def fetch_segment(
        self,
        *,
        msn: int,
        uri: str,
        declared_duration: float | None,
        discontinuity_before: bool = False,
        byterange: str | None = None,
    ) -> SampledSegment:
        requested_at = dt.datetime.now(dt.UTC)
        if byterange:
            length_text, _, offset_text = byterange.partition("@")
            start = int(offset_text) if offset_text.strip().isdigit() else 0
            length = int(length_text) if length_text.strip().isdigit() else 0
            result = await self.fetcher.fetch_range(uri, start=start, length=length)
        else:
            result = await self.fetcher.fetch(uri)
        completed_at = dt.datetime.now(dt.UTC)

        # The segment counts as attempted the moment it comes off the wire, before it is
        # parsed. Marking it afterwards meant a body the parser could not read was re-fetched
        # on every poll for the life of the session, and the rung never advanced past it.
        self.sampling.fetched_msns.add(msn)
        if len(self.sampling.fetched_msns) > self.max_tracked_msns:
            cutoff = sorted(self.sampling.fetched_msns)[: len(self.sampling.fetched_msns) // 2]
            self.sampling.fetched_msns.difference_update(cutoff)

        decrypted = self._decrypt(result.body)
        analysis = self._analyse(
            result,
            body=decrypted.body,
            encrypted=decrypted.encrypted,
            uri=uri,
            msn=msn,
            declared_duration=declared_duration,
        )
        analysis.drm_reason = decrypted.reason

        return SampledSegment(
            variant=self.sampling.variant,
            msn=msn,
            uri=uri,
            requested_at=requested_at,
            completed_at=completed_at,
            result=result,
            analysis=analysis,
            declared_duration=declared_duration,
            discontinuity_before=discontinuity_before,
            decoded_body=(self.sampling.init_body + decrypted.body if decrypted.ran else b""),
            drm_reason=decrypted.reason,
        )

    def _decrypt(self, body: bytes) -> DecryptedBody:
        """The bytes the parsers read, whether they are still encrypted, and why.

        A clear rendition passes through untouched. A protected one is decrypted when this run
        holds its key, and otherwise comes back encrypted carrying the reason — which the
        engine states as a finding rather than letting the rendition read as one that passed
        every bitstream check.
        """
        if not self.encrypted:
            return DecryptedBody(body=body, encrypted=False)
        if self.drm is None or not body:
            return DecryptedBody(body=body, encrypted=True)

        protection = self.drm.protection(self.sampling.variant)
        if protection is not None and not protection.info.protected:
            # `tenc` says this track carries no encryption, whatever the ladder declares.
            return DecryptedBody(body=body, encrypted=False)

        result = self.drm.decrypt(self.sampling.variant, body)
        if result.ok:
            return DecryptedBody(body=result.data, encrypted=False, ran=True)
        return DecryptedBody(body=body, encrypted=True, reason=result.reason)

    def _analyse(
        self,
        result: FetchResult,
        *,
        body: bytes,
        encrypted: bool,
        uri: str,
        msn: int,
        declared_duration: float | None,
    ) -> SegmentAnalysis:
        """Parse the body, and record a parser failure as a fact about that segment.

        The transport measurement — status, bytes, TTFB, total time — is valid whatever the
        bitstream turns out to contain. A parser that raises must not take the measurement
        down with it, and must not stop the rung being sampled.
        """
        try:
            return analyse(
                body,
                uri=uri,
                declared_duration=declared_duration,
                variant_id=self.sampling.variant,
                msn=msn,
                init_segment=self.sampling.init_segment,
                encrypted=encrypted,
            )
        except Exception as exc:
            log.exception(
                "Segment parse failed for %s msn %s (%s bytes from %s)",
                self.sampling.variant,
                msn,
                result.bytes_received,
                uri,
            )
            failed = SegmentAnalysis(
                uri=uri,
                container="unknown",
                byte_size=result.bytes_received,
                variant_id=self.sampling.variant,
                msn=msn,
                declared_duration=declared_duration,
                encrypted=encrypted,
            )
            failed.parse_error = f"The analyzer did not parse this segment: {type(exc).__name__}"
            return failed

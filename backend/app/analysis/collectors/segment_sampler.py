"""Segment sampler.

Every new segment on the lowest, middle and highest video rung is downloaded, plus every
segment of every audio rendition. Other rungs are sampled every Nth segment. Each download
is measured (bytes, TTFB, total time, throughput) and parsed, and the measurement is what the
Virtual Player Buffer replays.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from app.media.fmp4 import Fmp4Analysis, parse_init
from app.media.segment import SegmentAnalysis, analyse
from app.net.fetcher import Fetcher, FetchResult


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

    @property
    def available(self) -> bool:
        return self.result.ok and self.result.bytes_received > 0

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


@dataclass
class RungSampling:
    """Sampling policy and state for one rung."""

    variant: str
    full: bool
    nth: int = 3
    fetched_msns: set[int] = field(default_factory=set)
    init_segment: Fmp4Analysis | None = None
    init_uri: str | None = None

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
        max_tracked_msns: int = 2000,
    ) -> None:
        self.sampling = sampling
        self.fetcher = fetcher
        self.encrypted = encrypted
        self.max_tracked_msns = max_tracked_msns

    async def ensure_init(self, init_uri: str | None) -> FetchResult | None:
        """Fetch and parse the EXT-X-MAP target once per rung."""
        if not init_uri or self.sampling.init_uri == init_uri:
            return None
        result = await self.fetcher.fetch(init_uri)
        self.sampling.init_uri = init_uri
        if result.ok:
            self.sampling.init_segment = parse_init(result.body)
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

        analysis = analyse(
            result.body,
            uri=uri,
            declared_duration=declared_duration,
            variant_id=self.sampling.variant,
            msn=msn,
            init_segment=self.sampling.init_segment,
            encrypted=self.encrypted,
        )

        self.sampling.fetched_msns.add(msn)
        if len(self.sampling.fetched_msns) > self.max_tracked_msns:
            cutoff = sorted(self.sampling.fetched_msns)[: len(self.sampling.fetched_msns) // 2]
            self.sampling.fetched_msns.difference_update(cutoff)

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
        )

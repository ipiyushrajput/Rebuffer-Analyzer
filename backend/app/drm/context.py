"""One analysis run's view of protection: what the channel declares, and the keys for it.

This is what makes a DRM channel analyse like a clear one with nothing for an operator to
fill in. The run holds one of these; every rendition's sampler asks it the same two questions:

* **What is this rung protected with?** Answered from the initialisation segment's `tenc` box,
  which declares the scheme, the initialisation-vector size and the key identifier of the
  track itself — the authority, because a ladder can encrypt video and audio under different
  keys. The playlist's own declaration and the `pssh` box are the fallbacks, in that order.
* **Give me the key.** Answered from the shared `KeyStore`, which asks KeyOS once per key
  identifier for the life of the run however many segments and rungs want it.

Nothing is asked of the key server for a clear rendition, a rendition whose system is not
Widevine, or a rendition whose key has already been refused once. A run with no credentials
configured measures everything it can and states why the bitstream rules did not run, rather
than failing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.drm.cenc import CENC, TrackEncryption, read_track_encryption
from app.drm.cpix import DEFAULT_ENDPOINT, CpixCredentials, CpixError, KeyStore
from app.drm.decrypt import DecryptResult, decrypt_segment
from app.drm.detect import DrmInfo, extract_kid, normalise_kid
from app.net.fetcher import Fetcher

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RenditionProtection:
    """What one rung is protected with, and whether its key is in hand."""

    variant: str
    info: DrmInfo = field(default_factory=DrmInfo)
    track: TrackEncryption | None = None
    key_hex: str = ""
    # Counted so a report states how much of the rung the rules actually read.
    segments_decrypted: int = 0
    segments_failed: int = 0

    @property
    def decryptable(self) -> bool:
        """Whether a segment of this rung can be handed to the bitstream rules."""
        return bool(self.key_hex) and bool(self.track and self.track.supported)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"variant": self.variant, **self.info.as_dict()}
        if self.track is not None:
            payload["scheme"] = self.track.scheme
            payload["iv_size"] = self.track.iv_size
        payload["segments_decrypted"] = self.segments_decrypted
        payload["segments_failed"] = self.segments_failed
        return payload


class DrmContext:
    """The protection state of one analysis run, shared by every rendition's sampler."""

    def __init__(
        self,
        *,
        declared: DrmInfo | None = None,
        credentials: CpixCredentials | None = None,
        content_id: str = "rba",
        fetcher: Fetcher | None = None,
    ) -> None:
        self.declared = declared or DrmInfo()
        self.credentials = credentials or CpixCredentials()
        self.fetcher = fetcher
        self.store = KeyStore(credentials=self.credentials, content_id=content_id)
        self.renditions: dict[str, RenditionProtection] = {}
        # The first refusal from the key server, kept so the run reports it once rather than
        # once per rendition per segment.
        self.key_server_error = ""

    @property
    def protected(self) -> bool:
        return self.declared.protected

    @property
    def needs_key_server(self) -> bool:
        return self.declared.system.needs_key_server

    def protection(self, variant: str) -> RenditionProtection | None:
        return self.renditions.get(variant)

    def summary(self) -> dict[str, object]:
        """What a report and the UI read. No key material, ever."""
        return {
            **self.declared.as_dict(),
            "credentials": self.credentials.describe(),
            "key_requests": self.store.requests_made,
            "keys_held": len(self.store.all_keys()),
            "key_server_error": self.key_server_error,
            "renditions": [p.as_dict() for p in self.renditions.values()],
        }

    async def read_init(self, variant: str, init_segment: bytes) -> RenditionProtection:
        """What this rung is protected with, and its key, from its initialisation segment.

        Called once per rung, when its `EXT-X-MAP` target is fetched. A rung that turns out to
        be clear on a protected ladder — a packager commonly leaves one rendition in the open
        — comes back unprotected and is analysed without a key.
        """
        protection = RenditionProtection(variant=variant, info=DrmInfo(system=self.declared.system))
        self.renditions[variant] = protection

        track = read_track_encryption(init_segment)
        protection.track = track

        if not track.is_protected:
            # The ladder declares protection; this track carries none. The bitstream rules
            # read it as they read any clear rendition.
            protection.info = DrmInfo(reason="This rendition's track is not encrypted.")
            return protection

        kid = normalise_kid(track.default_kid) or extract_kid(init_segment) or self.declared.kid
        protection.info.kid = kid

        if track.scheme != CENC:
            protection.info.reason = (
                f"The track is encrypted with the {track.scheme} scheme, which this analyzer "
                "does not decrypt. Only cenc (AES-CTR) is decrypted."
            )
            return protection
        if not self.needs_key_server:
            protection.info.reason = (
                f"{self.declared.system.label} does not serve content keys over CPIX, so the "
                "payload of this rendition was not read."
            )
            return protection
        if not kid:
            protection.info.reason = (
                "No key identifier is declared by the playlist or carried by the "
                "initialisation segment, so no content key could be requested."
            )
            return protection
        if not self.credentials.configured:
            protection.info.reason = (
                "No CPIX credentials are configured on this analyzer, so no content key was "
                "requested. Set them in Settings → DRM."
            )
            return protection

        try:
            protection.key_hex = await self.store.key_for(kid, self.fetcher)
        except CpixError as exc:
            self.key_server_error = self.key_server_error or str(exc)
            protection.info.reason = str(exc)
            return protection
        except Exception as exc:
            message = f"The key server did not answer: {type(exc).__name__}."
            logger.exception("the CPIX request for %s failed", variant)
            self.key_server_error = self.key_server_error or message
            protection.info.reason = message
            return protection

        if not protection.key_hex:
            protection.info.reason = (
                f"The key server holds no key for {kid}, so the payload of this rendition was "
                "not read."
            )
            return protection

        protection.info.key_obtained = True
        return protection

    def decrypt(self, variant: str, body: bytes) -> DecryptResult:
        """One segment of one rung, decrypted if this run holds the key for it."""
        protection = self.renditions.get(variant)
        if protection is None:
            return DecryptResult(reason="This rendition's initialisation segment was not read.")
        if not protection.decryptable:
            return DecryptResult(reason=protection.info.reason or "No content key is held.")

        result = decrypt_segment(body, key_hex=protection.key_hex, track=protection.track)
        if result.ok:
            protection.segments_decrypted += 1
        else:
            protection.segments_failed += 1
        return result


def credentials_from_settings(values: dict[str, str]) -> CpixCredentials:
    """The CPIX credentials a deployment configured, from the settings block.

    The three PEM sources are a path or an HTTPS URL and are read at run time, so nothing here
    holds key material and nothing is written to the repository.
    """
    return CpixCredentials(
        client_cert=(values.get("cpix_client_cert") or "").strip(),
        client_key=(values.get("cpix_client_key") or "").strip(),
        server_cert=(values.get("cpix_server_cert") or "").strip(),
        endpoint=(values.get("cpix_endpoint") or DEFAULT_ENDPOINT).strip(),
    )

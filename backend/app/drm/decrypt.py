"""Turning an encrypted segment back into one the bitstream rules can read.

A CENC-protected fMP4 segment carries clear headers and encrypted sample payloads. The timing
checks read `tfdt` and `trun` and work on it untouched; the bitstream checks — decode errors,
SPS consistency, ADTS configuration, keyframe detection — read the payload and cannot.

Decryption needs two things the segment alone does not have: the content key, which
`app/drm/cpix.py` obtains, and the `moov` box from the initialisation segment, which carries
the track and protection headers. A media segment on its own has neither, which is why the
initialisation segment is prepended before the decrypt runs — the same thing a player does.

**The tool is ffmpeg**, which the analyzer already depends on and already reports the absence
of. `-decryption_key` decrypts CENC in place and `-c copy` writes the result without
re-encoding, so what the rules then read is the packager's own bitstream, not a transcode of
it. A host without ffmpeg loses DRM analysis the same way it loses the decode-error detector,
and says so rather than reporting a protected channel as clean.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass

from app.media.ffprobe import FFMPEG, FfprobeUnavailable, _run

logger = logging.getLogger(__name__)

# A segment that decrypts to far less than it started as did not decrypt: a wrong key turns
# the payload into noise the demuxer discards, and the output shrinks. The threshold is
# deliberately loose — the encrypted form carries protection boxes the clear one drops.
MIN_SIZE_RATIO = 0.70

DECRYPT_TIMEOUT_S = 45.0


class DecryptError(Exception):
    """The segment could not be decrypted. The message is what a finding states."""


@dataclass(slots=True)
class DecryptResult:
    """What came back, and enough about it to report why the rules did or did not run."""

    data: bytes = b""
    ok: bool = False
    reason: str = ""
    # What the decrypt cost, so a long aging run can be judged on more than its findings.
    duration_ms: float = 0.0

    @property
    def size(self) -> int:
        return len(self.data)


def available() -> bool:
    """Whether this host can decrypt at all."""
    return bool(shutil.which(FFMPEG))


async def decrypt_segment(
    data: bytes,
    *,
    key_hex: str,
    init_segment: bytes = b"",
    timeout: float = DECRYPT_TIMEOUT_S,
) -> DecryptResult:
    """One encrypted fMP4 segment, decrypted with its content key.

    The initialisation segment is prepended so the decrypt has the `moov` box it needs; a
    media segment carries only `moof` and `mdat` and is not a readable file on its own.

    Failure is reported, never raised into the analysis loop: a segment that will not decrypt
    is one the bitstream rules skip, and the run carries on measuring everything else.
    """
    if not key_hex:
        return DecryptResult(reason="No content key was obtained for this rendition.")
    if not available():
        raise FfprobeUnavailable("ffmpeg is not installed on the analyzer host")
    if not data:
        return DecryptResult(reason="The segment body is empty.")

    payload = init_segment + data
    argv = [
        FFMPEG,
        "-v",
        "error",
        "-hide_banner",
        "-decryption_key",
        key_hex,
        "-i",
        "pipe:0",
        "-c",
        "copy",
        # Fragmented output, so a segment that arrived as a fragment leaves as one and the
        # demuxer downstream sees the shape it expects.
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]

    started = time.perf_counter()
    code, out, err = await _run(argv, timeout=timeout, stdin=payload)
    elapsed = (time.perf_counter() - started) * 1000

    if code != 0:
        detail = err.decode("utf-8", errors="replace").strip().splitlines()
        return DecryptResult(
            reason=f"ffmpeg could not decrypt the segment: {detail[-1] if detail else 'no output'}",
            duration_ms=elapsed,
        )
    if not out:
        return DecryptResult(reason="The decrypt produced no output.", duration_ms=elapsed)

    ratio = len(out) / max(len(payload), 1)
    if ratio < MIN_SIZE_RATIO:
        # The usual cause is the wrong key: the payload decrypts to noise, the demuxer drops
        # what it cannot read, and the output is a fraction of the input. Reporting that is
        # better than handing the rules a segment that will read as corruption.
        return DecryptResult(
            reason=(
                f"The decrypted segment is {ratio * 100:.0f}% of the size of the encrypted one, "
                f"below the {MIN_SIZE_RATIO * 100:.0f}% a sound decrypt produces."
            ),
            duration_ms=elapsed,
        )

    return DecryptResult(data=out, ok=True, duration_ms=elapsed)

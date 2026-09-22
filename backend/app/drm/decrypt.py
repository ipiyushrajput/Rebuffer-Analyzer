"""Turning an encrypted segment back into one the bitstream rules can read.

A CENC-protected CMAF segment carries clear headers and encrypted sample payloads. The timing
checks read `tfdt` and `trun` and work on it untouched; the bitstream checks — decode errors,
SPS consistency, ADTS configuration, keyframe detection — read the payload and cannot.

Decryption needs two things the media segment alone does not have: the content key, which
`app/drm/cpix.py` obtains from KeyOS, and the `tenc` box from the initialisation segment,
which declares the key identifier, the initialisation-vector size and the scheme.

**The decrypt is done in this process**, by `app/drm/cenc.py`, rather than by shelling out to
ffmpeg or mp4decrypt. The analyzer already holds the segment in memory and hands it straight
to the parsers; a subprocess per segment would put a temporary file and a process launch
inside the measurement loop of a run that fetches a segment every few seconds on every
rendition, for days. It also removes a second thing for a host to have installed: a deployment
with ffmpeg missing loses the decode-error detector, and would otherwise lose every DRM
channel with it.

What comes back is the packager's own bitstream — only the encrypted byte ranges are
rewritten, and every box, sample header and NAL length prefix is copied through — so the
parsers downstream read exactly what a player would decode.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from app.drm.cenc import CBCS, CencError, TrackEncryption, decrypt_fragment

logger = logging.getLogger(__name__)


class DecryptError(Exception):
    """The segment could not be decrypted. The message is what a finding states."""


# The two backends, named in every result so a reader knows which produced the bytes.
IN_PROCESS = "cenc"
MP4DECRYPT = "mp4decrypt"

MP4DECRYPT_BINARY = "mp4decrypt"


@dataclass(slots=True)
class DecryptResult:
    """What came back, and enough about it to report why the rules did or did not run."""

    data: bytes = b""
    ok: bool = False
    reason: str = ""
    # What the decrypt cost, so a long aging run can be judged on more than its findings.
    duration_ms: float = 0.0
    # Which backend produced these bytes. Empty on a failure that never reached one.
    backend: str = ""

    @property
    def size(self) -> int:
        return len(self.data)


def mp4decrypt_path() -> str | None:
    """Where Bento4's `mp4decrypt` is on this host, or None.

    It is the only way `cbcs` is decrypted: that scheme is AES-CBC with a crypt/skip pattern,
    a different cipher from the AES-CTR `app/drm/cenc.py` implements. A host without it reads
    a `cbcs` channel's transport and timing and says the payload was not read, rather than
    attempting a cipher it does not have.
    """
    return shutil.which(MP4DECRYPT_BINARY)


def backends() -> dict[str, bool]:
    """What this host can decrypt with. Reported by `/api/health`."""
    return {IN_PROCESS: available(), MP4DECRYPT: mp4decrypt_path() is not None}


def decrypt_with_mp4decrypt(
    data: bytes,
    *,
    key_hex: str,
    kid_hex: str,
    init_segment: bytes = b"",
    timeout_s: float = 20.0,
) -> DecryptResult:
    """One fragment through Bento4, for the scheme the in-process path refuses.

    `mp4decrypt` reads a file and writes a file, so the fragment goes to a temporary
    directory with its initialisation segment in front of it — Bento4 needs the `tenc` box to
    know what it is decrypting — and the directory goes away whether it succeeded or not. The
    key is passed on the command line as `--key <KID>:<KEY>`, which is the only interface it
    has; it is never logged and never written to a file.
    """
    started = time.perf_counter()
    if not key_hex or not kid_hex:
        return DecryptResult(reason="No content key and key identifier pair for this rendition.")
    binary = mp4decrypt_path()
    if binary is None:
        return DecryptResult(
            reason=(
                "This track uses the cbcs scheme, which needs Bento4's mp4decrypt, and that "
                "binary is not installed on this analyzer host."
            )
        )

    with tempfile.TemporaryDirectory(prefix="rba-decrypt-") as workdir:
        source = Path(workdir) / "in.mp4"
        target = Path(workdir) / "out.mp4"
        source.write_bytes(init_segment + data)
        try:
            # A fixed argv with no shell: the only variable parts are paths this function
            # created and the key, which never reaches a log or a file.
            completed = subprocess.run(
                [binary, "--key", f"{kid_hex}:{key_hex}", str(source), str(target)],
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return DecryptResult(
                reason=f"mp4decrypt did not finish within {timeout_s:.0f} s.",
                duration_ms=(time.perf_counter() - started) * 1000,
                backend=MP4DECRYPT,
            )
        elapsed = (time.perf_counter() - started) * 1000
        if completed.returncode != 0 or not target.exists():
            # stderr can echo the command line, and the command line carries the key.
            return DecryptResult(
                reason=f"mp4decrypt exited {completed.returncode} without producing output.",
                duration_ms=elapsed,
                backend=MP4DECRYPT,
            )
        plain = target.read_bytes()

    expected = len(init_segment) + len(data)
    if len(plain) < expected * 0.8:
        # Decryption rewrites sample payloads in place; a much smaller file means Bento4
        # dropped tracks rather than decrypting them, and it is not the packager's bitstream.
        return DecryptResult(
            reason=(
                f"mp4decrypt produced {len(plain)} bytes from {expected}, under the 80% a "
                "decrypt of this fragment has to keep."
            ),
            duration_ms=elapsed,
            backend=MP4DECRYPT,
        )
    return DecryptResult(data=plain, ok=True, duration_ms=elapsed, backend=MP4DECRYPT)


def available() -> bool:
    """Whether this host can decrypt at all.

    The cipher is a library rather than a binary, so this is true wherever the backend's own
    dependencies installed. It stays a function because the caller reports the answer, and a
    broken install has to read as "no decryption" rather than as a clean channel.
    """
    try:
        import Crypto.Cipher.AES  # noqa: F401
    except Exception:
        return False
    return True


def decrypt_segment(
    data: bytes,
    *,
    key_hex: str,
    track: TrackEncryption | None = None,
) -> DecryptResult:
    """One encrypted CMAF fragment, decrypted with its content key.

    Failure is reported, never raised into the analysis loop: a segment that will not decrypt
    is one the bitstream rules skip, and the run carries on measuring everything else.
    """
    if not key_hex:
        return DecryptResult(reason="No content key was obtained for this rendition.")
    if not data:
        return DecryptResult(reason="The segment body is empty.")
    if not available():
        return DecryptResult(
            reason=(
                "The AES implementation this analyzer decrypts with is not installed on this "
                "host, so protected segments are not being read."
            )
        )
    if track is not None and track.scheme == CBCS:
        return DecryptResult(
            reason=(
                f"The track is encrypted with the cbcs scheme "
                f"({track.crypt_byte_block}:{track.skip_byte_block} pattern), which this "
                "analyzer does not decrypt. Only cenc (AES-CTR) is decrypted."
            )
        )

    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        return DecryptResult(reason="The content key obtained for this rendition is not hex.")

    started = time.perf_counter()
    try:
        plain = decrypt_fragment(data, key=key, iv_size=track.iv_size if track else 8)
    except CencError as exc:
        return DecryptResult(reason=str(exc), duration_ms=(time.perf_counter() - started) * 1000)
    except Exception as exc:
        logger.exception("a protected segment of %s bytes did not decrypt", len(data))
        return DecryptResult(
            reason=f"The segment did not decrypt: {type(exc).__name__}.",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    elapsed = (time.perf_counter() - started) * 1000

    if len(plain) != len(data):
        # CENC decryption is length-preserving by construction. A different length means the
        # box walk and the sample map disagreed, and the result is not the packager's
        # bitstream.
        return DecryptResult(
            reason=(
                f"The decrypted segment is {len(plain)} bytes where the encrypted one was "
                f"{len(data)}; CENC decryption does not change a segment's length."
            ),
            duration_ms=elapsed,
        )
    # A fragment that comes back byte for byte identical carried no `senc`, so there was
    # nothing in it to decrypt and its bytes are already the ones a decoder reads.
    return DecryptResult(data=plain, ok=True, duration_ms=elapsed, backend=IN_PROCESS)

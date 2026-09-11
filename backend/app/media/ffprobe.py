"""ffprobe / ffmpeg helpers.

The pure-Python parsers cover every check RBA needs on a well-formed segment. ffprobe is
used for two things they cannot do: reading a segment a parser rejects, and decoding it to
surface decoder errors. When the binary is absent the caller records a definite INFO
finding saying the check did not run and why — it never infers a result.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from typing import Any

FFPROBE = "ffprobe"
FFMPEG = "ffmpeg"


@dataclass(slots=True)
class BinaryStatus:
    ffprobe: str | None
    ffmpeg: str | None

    @property
    def available(self) -> bool:
        return bool(self.ffprobe and self.ffmpeg)


def binaries() -> BinaryStatus:
    return BinaryStatus(ffprobe=shutil.which(FFPROBE), ffmpeg=shutil.which(FFMPEG))


class FfprobeUnavailable(RuntimeError):
    """Raised when ffprobe is not installed. Callers turn this into a definite INFO finding."""


async def _run(
    argv: list[str], *, timeout: float, stdin: bytes | None = None
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(input=stdin), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, out, err


async def probe_url(
    url: str, *, timeout: float = 30.0, user_agent: str | None = None
) -> dict[str, Any]:
    """Run ffprobe against a URL. TLS verification stays off for consistency with the fetcher."""
    if not shutil.which(FFPROBE):
        raise FfprobeUnavailable("ffprobe is not installed on the analyzer host")
    argv = [
        FFPROBE,
        "-v",
        "error",
        "-hide_banner",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        "-tls_verify",
        "0",
    ]
    if user_agent:
        argv += ["-user_agent", user_agent]
    argv += [url]
    code, out, err = await _run(argv, timeout=timeout)
    if code != 0:
        return {"error": err.decode("utf-8", errors="replace").strip(), "streams": [], "format": {}}
    return json.loads(out or b"{}")


async def probe_bytes(data: bytes, *, timeout: float = 20.0) -> dict[str, Any]:
    """Run ffprobe on an in-memory segment via stdin."""
    if not shutil.which(FFPROBE):
        raise FfprobeUnavailable("ffprobe is not installed on the analyzer host")
    argv = [
        FFPROBE,
        "-v",
        "error",
        "-hide_banner",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        "-show_packets",
        "-read_intervals",
        "%+#200",
        "-i",
        "pipe:0",
    ]
    code, out, err = await _run(argv, timeout=timeout, stdin=data)
    if code != 0:
        return {"error": err.decode("utf-8", errors="replace").strip(), "streams": [], "format": {}}
    return json.loads(out or b"{}")


async def decode_errors(data: bytes, *, timeout: float = 30.0) -> list[str]:
    """Decode a segment and return every decoder error line ffmpeg emitted."""
    if not shutil.which(FFMPEG):
        raise FfprobeUnavailable("ffmpeg is not installed on the analyzer host")
    argv = [FFMPEG, "-v", "error", "-hide_banner", "-i", "pipe:0", "-f", "null", "-"]
    _code, _out, err = await _run(argv, timeout=timeout, stdin=data)
    return [line for line in err.decode("utf-8", errors="replace").splitlines() if line.strip()]


async def detect_black_and_freeze(
    data: bytes, *, black_min_s: float = 0.5, freeze_min_s: float = 2.0, timeout: float = 60.0
) -> dict[str, list[dict[str, float]]]:
    """Run the blackdetect and freezedetect filters over a segment."""
    if not shutil.which(FFMPEG):
        raise FfprobeUnavailable("ffmpeg is not installed on the analyzer host")
    argv = [
        FFMPEG,
        "-v",
        "info",
        "-hide_banner",
        "-i",
        "pipe:0",
        "-vf",
        f"blackdetect=d={black_min_s}:pic_th=0.98,freezedetect=n=-60dB:d={freeze_min_s}",
        "-an",
        "-f",
        "null",
        "-",
    ]
    _code, _out, err = await _run(argv, timeout=timeout, stdin=data)
    text = err.decode("utf-8", errors="replace")
    black: list[dict[str, float]] = []
    freeze: list[dict[str, float]] = []
    for line in text.splitlines():
        if "black_start" in line:
            black.append(_kv_floats(line, ("black_start", "black_end", "black_duration")))
        elif "freeze_start" in line:
            freeze.append(_kv_floats(line, ("freeze_start",)))
        elif "freeze_duration" in line:
            freeze.append(_kv_floats(line, ("freeze_duration", "freeze_end")))
    return {"black": black, "freeze": freeze}


def _kv_floats(line: str, keys: tuple[str, ...]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in keys:
        marker = f"{key}:"
        if marker in line:
            tail = line.split(marker, 1)[1].strip().split()[0]
            try:
                out[key] = float(tail)
            except ValueError:
                continue
    return out


async def trace_headers(data: bytes, *, timeout: float = 30.0) -> str:
    """Dump parsed bitstream headers. Used to validate the pure-Python SPS decoder."""
    if not shutil.which(FFMPEG):
        raise FfprobeUnavailable("ffmpeg is not installed on the analyzer host")
    argv = [
        FFMPEG,
        "-v",
        "trace",
        "-hide_banner",
        "-i",
        "pipe:0",
        "-bsf:v",
        "trace_headers",
        "-f",
        "null",
        "-",
    ]
    _code, _out, err = await _run(argv, timeout=timeout, stdin=data)
    return err.decode("utf-8", errors="replace")

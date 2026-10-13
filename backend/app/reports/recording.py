"""A recording, as one archive a troubleshooting engineer can open and play.

Built from the folder `app.analysis.recorder` wrote and streamed as it is built: a long
recording is gigabytes, so it is neither held in memory nor copied to a temporary file. Media is
stored, not compressed — video does not compress — and the ZIP is written in streaming form,
which every unzip tool reads.

    <clip>/<layer>_<variant>.<ts|mp4>              the rendition as served: the init segment
                                                   and every segment, joined in order — plays
                                                   in VLC, ffplay and any browser that plays it
    <clip>/decrypted/<layer>_<variant>.mp4         a protected rendition as decrypted, when this
                                                   run held its key
    <clip>/playable/<video>+<audio>.mp4            a demuxed rendition with its audio, joined by
                                                   ffmpeg when it is installed
    <clip>/network/                                every playlist poll and segment fetch inside
                                                   the clip, as files, a CSV and a HAR
    recording.json                                 every segment: MSN, URI, time, status, file
    README.txt                                     what is here, and what is not, and why

A realtime recording is one clip, `full`. An aging recording is one clip per incident, from the
pre-roll before it began to the post-roll after it ended, overlapping incidents merged.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import subprocess
import tempfile
import zipfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.analysis.recorder import INCIDENTS, read_summary
from app.media import ffprobe
from app.reports import network_log

CHUNK = 1024 * 1024
TOOL_NAME = "StreamLens"


class _Pipe:
    """A write-only file the ZIP is written into, emptied into the response as it fills.

    It has `tell` and no `seek`, which is what makes `zipfile` write the streaming form.
    """

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.position = 0

    def write(self, data: bytes) -> int:
        self.chunks.append(bytes(data))
        self.position += len(data)
        return len(data)

    def tell(self) -> int:
        return self.position

    def flush(self) -> None:
        return None

    def drain(self) -> bytes:
        data = b"".join(self.chunks)
        self.chunks = []
        return data


@dataclass
class Clip:
    name: str
    start: dt.datetime | None
    end: dt.datetime | None
    label: str
    segments: list[dict[str, Any]] = field(default_factory=list)


def _moment(value: Any) -> dt.datetime | None:
    if not value:
        return None
    moment = dt.datetime.fromisoformat(str(value))
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def clips_of(summary: dict[str, Any]) -> list[Clip]:
    """The clips a recording is cut into: one for a full recording, one per incident window."""
    segments = summary.get("segments") or []
    if summary.get("mode") != INCIDENTS:
        return [Clip(name="full", start=None, end=None, label="the whole run", segments=segments)]

    pre = dt.timedelta(seconds=float(summary.get("preroll_s") or 0))
    post = dt.timedelta(seconds=float(summary.get("postroll_s") or 0))
    ended = _moment(summary.get("ended_at")) or dt.datetime.now(dt.UTC)
    spans: list[tuple[dt.datetime, dt.datetime, str]] = []
    for window in summary.get("windows") or []:
        start = _moment(window.get("start"))
        if start is None:
            continue
        end = _moment(window.get("end")) or ended
        spans.append((start - pre, end + post, window.get("variant") or "ladder"))
    spans.sort()
    merged: list[list[Any]] = []
    for start, end, variant in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2].add(variant)
        else:
            merged.append([start, end, {variant}])

    clips: list[Clip] = []
    for index, (start, end, variants) in enumerate(merged, start=1):
        inside = [
            s for s in segments if (at := _moment(s.get("at"))) is not None and start <= at <= end
        ]
        clips.append(
            Clip(
                name=f"clip_{index:02d}_{start.strftime('%Y%m%dT%H%M%SZ')}",
                start=start,
                end=end,
                label=(
                    f"incident on {', '.join(sorted(variants))}, "
                    f"{start.isoformat()} to {end.isoformat()}"
                ),
                segments=inside,
            )
        )
    return clips


def _streams(segments: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for segment in segments:
        if not segment.get("file"):
            continue
        out.setdefault((segment["layer"], segment["variant"]), []).append(segment)
    for items in out.values():
        items.sort(key=lambda s: s["msn"])
    return out


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_.@+" else "_" for ch in text)[:100] or "x"


def _read(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            yield chunk


def _joined(directory: Path, items: list[dict[str, Any]], *, decrypted: bool) -> Iterator[bytes]:
    """The init segment (once per change) and every segment in MSN order, as one stream."""
    current_init: str | None = None
    for item in items:
        relative = item.get("decrypted_file") if decrypted else item.get("file")
        if not relative:
            continue
        init = item.get("init")
        if init and init != current_init and (directory / init).exists():
            current_init = init
            yield from _read(directory / init)
        path = directory / relative
        if path.exists():
            yield from _read(path)


def _write_stream(target: Path, chunks: Iterator[bytes]) -> int:
    size = 0
    with target.open("wb") as handle:
        for chunk in chunks:
            handle.write(chunk)
            size += len(chunk)
    return size


def _mux(video: Path, audio: Path, output: Path) -> str:
    """Video and audio into one MP4 with ffmpeg, copying both streams. Empty on success."""
    binary = ffprobe.binaries().ffmpeg
    if not binary:
        return "ffmpeg is not installed on the analyzer host."
    completed = subprocess.run(
        [
            binary,
            "-v",
            "error",
            "-y",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            str(output),
        ],
        capture_output=True,
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        return completed.stderr.decode("utf-8", errors="replace")[-400:] or "ffmpeg failed."
    return ""


def _readme(summary: dict[str, Any], clips: list[Clip], notes: list[str]) -> str:
    segments = summary.get("segments") or []
    lines = [
        f"{TOOL_NAME} stream recording",
        "",
        f"Job          : {summary.get('job_id')}",
        f"Mode         : {summary.get('mode')}"
        + (
            " (incident clips: pre-roll before, post-roll after)"
            if summary.get("mode") == INCIDENTS
            else ""
        ),
        f"Segments     : {len(segments)} kept, {summary.get('deleted', 0)} outside every incident "
        f"removed, {summary.get('failed', 0)} not fetched",
        f"Clips        : {len(clips)}",
    ]
    if summary.get("stopped_reason"):
        lines += ["", f"Recording stopped early: {summary['stopped_reason']}"]
    lines += [
        "",
        "Each clip holds every recorded rendition joined into one file in media-sequence order,",
        "the initialisation segment first for fragmented MP4. Play it in VLC or ffplay.",
        "decrypted/ holds a protected rendition as decrypted with the key this run obtained; its",
        "initialisation segment still declares the protection, which VLC and ffplay ignore.",
        "network/ holds every playlist poll and segment fetch inside the clip.",
        "recording.json lists every segment with its MSN, source URI, time and status.",
        "",
    ]
    for clip in clips:
        lines.append(f"{clip.name}: {clip.label} — {len(clip.segments)} segment(s)")
    if notes:
        lines += ["", "Notes:"] + [f"  - {note}" for note in notes]
    return "\n".join(lines) + "\n"


async def stream_recording(root: Path, job_id: str) -> AsyncIterator[bytes]:
    """The recording archive of a job, chunk by chunk."""
    summary = read_summary(root, job_id)
    if summary is None:
        return
    directory = root / job_id
    clips = clips_of(summary)
    pipe = _Pipe()
    notes: list[str] = []
    archive = zipfile.ZipFile(pipe, "w", zipfile.ZIP_STORED)  # type: ignore[call-overload]

    def entry(name: str) -> zipfile.ZipInfo:
        now = dt.datetime.now(dt.UTC)
        info = zipfile.ZipInfo(
            name, (now.year, now.month, now.day, now.hour, now.minute, now.second)
        )
        info.compress_type = zipfile.ZIP_STORED
        return info

    try:
        for clip in clips:
            streams = _streams(clip.segments)
            joined: dict[tuple[str, str], Path] = {}
            with tempfile.TemporaryDirectory(dir=directory) as scratch:
                for (layer, variant), items in streams.items():
                    extension = "mp4" if any(i.get("init") for i in items) else "ts"
                    name = f"{clip.name}/{_slug(layer)}_{_slug(variant)}.{extension}"
                    with archive.open(entry(name), "w", force_zip64=True) as handle:
                        for chunk in _joined(directory, items, decrypted=False):
                            handle.write(chunk)
                            yield pipe.drain()
                    if any(i.get("decrypted_file") for i in items):
                        name = f"{clip.name}/decrypted/{_slug(layer)}_{_slug(variant)}.mp4"
                        with archive.open(entry(name), "w", force_zip64=True) as handle:
                            for chunk in _joined(directory, items, decrypted=True):
                                handle.write(chunk)
                                yield pipe.drain()
                    elif any(i.get("drm_reason") for i in items):
                        reason = next(i["drm_reason"] for i in items if i.get("drm_reason"))
                        notes.append(f"{clip.name} {variant} is kept as served only: {reason}")

                # A demuxed rendition plays with its audio only once the two are joined.
                kinds = {(s["layer"], s["variant"]): s.get("kind") for s in clip.segments}
                audio = next((key for key in streams if kinds.get(key) == "audio"), None)
                videos = [
                    key
                    for key, items in streams.items()
                    if kinds.get(key) == "video" and any(i.get("init") for i in items)
                ]
                if audio is not None and videos:
                    if not ffprobe.binaries().ffmpeg:
                        notes.append(
                            "Video and audio are separate renditions here and ffmpeg is not "
                            "installed, so they are not joined into one playable file."
                        )
                    else:
                        scratch_dir = Path(scratch)
                        for key in [audio, *videos]:
                            use_decrypted = any(i.get("decrypted_file") for i in streams[key])
                            path = scratch_dir / f"{_slug(key[0])}_{_slug(key[1])}.mp4"
                            await asyncio.to_thread(
                                _write_stream,
                                path,
                                _joined(directory, streams[key], decrypted=use_decrypted),
                            )
                            joined[key] = path
                        for key in videos:
                            output = scratch_dir / f"{_slug(key[1])}+{_slug(audio[1])}.mp4"
                            problem = await asyncio.to_thread(
                                _mux, joined[key], joined[audio], output
                            )
                            if problem:
                                notes.append(
                                    f"{clip.name} {key[1]}: ffmpeg did not join it: {problem}"
                                )
                                continue
                            name = f"{clip.name}/playable/{output.name}"
                            with archive.open(entry(name), "w", force_zip64=True) as handle:
                                for chunk in _read(output):
                                    handle.write(chunk)
                                    yield pipe.drain()

            # The network record of the clip's own minutes.
            await network_log.write_into(
                archive, job_id, start=clip.start, end=clip.end, prefix=f"{clip.name}/network/"
            )
            yield pipe.drain()

        archive.writestr("recording.json", json.dumps(summary, indent=1), zipfile.ZIP_DEFLATED)
        archive.writestr("README.txt", _readme(summary, clips, notes), zipfile.ZIP_DEFLATED)
    finally:
        with contextlib.suppress(Exception):
            archive.close()
    yield pipe.drain()


def recording_state(root: Path, job_id: str) -> dict[str, Any]:
    """Whether a job has a recording, and what it holds — for the UI's download button."""
    summary = read_summary(root, job_id)
    if summary is None:
        return {"available": False}
    segments = summary.get("segments") or []
    return {
        "available": bool(segments),
        "mode": summary.get("mode"),
        "segments": len(segments),
        "bytes": sum(int(s.get("bytes") or 0) for s in segments),
        "clips": len(clips_of(summary)),
        "decrypted": sum(1 for s in segments if s.get("decrypted_file")),
        "stopped_reason": summary.get("stopped_reason") or "",
    }

"""What a finished run leaves in object storage, and how a download finds it.

When a run ends, its archives are written once into the run's folder (`app/storage/keys.py`):

* `archives/network-log.zip` — every poll and every fetch of the run;
* `archives/evidence.zip` — findings, playlists and, while the session is still in memory,
  the segment bytes it sampled (a bundle rebuilt later no longer has those);
* `recording/<full|clips>/` — the raw recording, and `recording.zip` beside it.

The recording is moved, not copied: once its files are in storage the copy under
`RBA_RECORDINGS_DIR` is removed, so the analyzer host holds a recording only while the run is
writing it. Every archive is written to a temporary file first and uploaded in one call, which
S3 turns into a multipart upload for a large one.

A download asks storage first: an archived file is a CloudFront URL (S3) or a stream from
disk (local). A run still going, or one finished before archiving existed, is built on
request exactly as before.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi.responses import RedirectResponse, Response, StreamingResponse

from app.analysis import recorder as recorder_module
from app.config import get_settings
from app.reports import network_log
from app.storage import PINNED, STANDARD, get_storage, keys, runs

logger = logging.getLogger(__name__)

NETWORK_LOG = "network-log.zip"
EVIDENCE = "evidence.zip"
RECORDING_ZIP = "recording.zip"
RECORDING_SUMMARY = recorder_module.SUMMARY
RECORDING_INDEX = recorder_module.INDEX


@dataclass
class ArchiveStats:
    """Counted for the metrics endpoint: archiving that fails is never silent."""

    archived: int = 0
    failed: int = 0
    last_error: str = ""
    uploaded_bytes: int = 0
    last_seconds: float = 0.0
    failures_by_kind: dict[str, int] = field(default_factory=dict)


STATS = ArchiveStats()


def recording_mode(summary: dict[str, Any] | None) -> str:
    return str((summary or {}).get("mode") or recorder_module.FULL)


def recording_retention(mode: str) -> str:
    return PINNED if mode == recorder_module.INCIDENTS else STANDARD


async def _upload_stream(
    key: str, chunks: AsyncIterator[bytes], *, retention: str, filename: str
) -> int:
    with tempfile.TemporaryDirectory(prefix="rba-archive-") as scratch:
        path = Path(scratch) / filename
        with path.open("wb") as handle:
            async for chunk in chunks:
                handle.write(chunk)
        if path.stat().st_size == 0:
            return 0
        stored = await get_storage().put_file(
            key, path, content_type="application/zip", retention=retention, filename=filename
        )
        return stored.size


async def _step(kind: str, run: Callable[[], Awaitable[int]]) -> int:
    try:
        size = await run()
    except Exception as exc:
        STATS.failed += 1
        STATS.failures_by_kind[kind] = STATS.failures_by_kind.get(kind, 0) + 1
        STATS.last_error = f"{kind}: {type(exc).__name__}"
        logger.exception("archiving the %s failed", kind)
        return 0
    STATS.uploaded_bytes += size
    return size


async def archive_run(handle: Any) -> dict[str, int]:
    """Write a finished run's archives into its storage folder. Never raises."""
    settings = get_settings()
    if not settings.rba_archive_artifacts:
        return {}
    started = time.monotonic()
    job_id = handle.id
    prefix = await runs.prefix_for(job_id)
    written: dict[str, int] = {}

    async def network() -> int:
        return await _upload_stream(
            keys.archive_key(prefix, NETWORK_LOG),
            network_log.stream_archive(job_id),
            retention=PINNED,
            filename=f"{keys.slug(handle.channel_name)}-network-log.zip",
        )

    async def evidence() -> int:
        from app.reports import service

        data = await service.build_evidence_bundle(handle)
        stored = await get_storage().put_bytes(
            keys.archive_key(prefix, EVIDENCE),
            data,
            content_type="application/zip",
            retention=PINNED,
            filename=f"{keys.slug(handle.channel_name)}-evidence.zip",
        )
        return stored.size

    if await network_log.poll_count(job_id) > 0:
        written[NETWORK_LOG] = await _step("network-log", network)
    if handle.session is not None:
        written[EVIDENCE] = await _step("evidence", evidence)
    written[RECORDING_ZIP] = await _step(
        "recording", lambda: archive_recording(job_id, prefix, handle.channel_name)
    )

    STATS.archived += 1
    STATS.last_seconds = time.monotonic() - started
    return written


async def archive_recording(job_id: str, prefix: str, channel_name: str | None) -> int:
    """Move a run's recording into storage: the playable archive, then the raw files."""
    from app.reports.recording import stream_recording

    root = get_settings().rba_recordings_dir
    directory = root / job_id
    summary = recorder_module.read_summary(root, job_id)
    if summary is None or not directory.is_dir():
        return 0
    mode = recording_mode(summary)
    retention = recording_retention(mode)
    storage = get_storage()

    size = await _upload_stream(
        keys.recording_key(prefix, mode, RECORDING_ZIP),
        stream_recording(root, job_id),
        retention=retention,
        filename=f"{keys.slug(channel_name)}-recording.zip",
    )
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        stored = await storage.put_file(
            keys.recording_key(prefix, mode, relative),
            path,
            content_type="application/json" if path.suffix in (".json", ".jsonl") else "video/mp2t",
            retention=retention,
        )
        size += stored.size
    # Everything is in storage: the analyzer host keeps no copy.
    shutil.rmtree(directory, ignore_errors=True)
    return size


# -- reading back ----------------------------------------------------------------------------


async def stored_summary(job_id: str, mode_hint: str | None = None) -> dict[str, Any] | None:
    """The recording summary of a run whose recording is in storage."""
    prefix = await runs.known_prefix(job_id)
    if not prefix:
        return None
    storage = get_storage()
    modes = [mode_hint] if mode_hint else [recorder_module.FULL, recorder_module.INCIDENTS]
    for mode in modes:
        data = await storage.get_bytes(keys.recording_key(prefix, mode, RECORDING_SUMMARY))
        if data is not None:
            loaded: dict[str, Any] = json.loads(data)
            loaded["_mode"] = mode
            return loaded
    return None


async def find(job_id: str, name: str) -> str | None:
    """The storage key of an archived file of this run, when there is one."""
    prefix = await runs.known_prefix(job_id)
    if not prefix:
        return None
    storage = get_storage()
    if name == RECORDING_ZIP:
        candidates = [
            keys.recording_key(prefix, mode, RECORDING_ZIP)
            for mode in (recorder_module.FULL, recorder_module.INCIDENTS)
        ]
    else:
        candidates = [keys.archive_key(prefix, name)]
    for key in candidates:
        if await storage.size(key) is not None:
            return key
    return None


def stored_response(key: str, *, media_type: str, filename: str) -> Response:
    """Answer a download from storage: a CloudFront URL when there is one, else a stream."""
    storage = get_storage()
    url = storage.download_url(key)
    if url:
        # 307 keeps the method; the browser follows it straight to the CDN.
        return RedirectResponse(url, status_code=307)
    return StreamingResponse(
        storage.iter_bytes(key),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def recording_info(job_id: str) -> dict[str, Any]:
    """Whether a run was recorded and what its recording holds — on disk or in storage."""
    from app.reports.recording import recording_state

    state = recording_state(get_settings().rba_recordings_dir, job_id)
    if state.get("available"):
        return state
    summary = await stored_summary(job_id)
    if summary is None:
        return state
    segments = summary.get("segments") or []
    return {
        "available": True,
        "mode": summary.get("mode") or summary.get("_mode"),
        "segments": len(segments),
        "bytes": sum(int(s.get("bytes") or 0) for s in segments),
        "stored": True,
    }

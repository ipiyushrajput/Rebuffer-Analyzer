"""Stream recording: every segment of every analysed rendition, written to disk as it is listed.

The sampler fetches what the rules need — a rung sampled every third segment, a session that
stops after its sample budget — which is not a recording. The recorder downloads every segment
each rendition's playlist lists, in its own worker per rendition so a slow segment never holds
up a poll, and reuses the bytes when the sampler already fetched the same segment.

Files are written under `RBA_RECORDINGS_DIR/<job id>/`:

    <layer>/<variant>/init_<hash>.<ext>        each distinct EXT-X-MAP body
    <layer>/<variant>/<msn>.<ext>              each segment, exactly as served
    <layer>/<variant>/decrypted/<msn>.<ext>    the decrypted fragment, for a protected rung
                                               whose key this run holds
    index.jsonl                                one line per segment as it is written
    recording.json                             the summary, written when the run ends

**Two modes.** `full` (realtime) keeps everything. `incidents` (aging) keeps a rolling pre-roll
and deletes what falls out of it unless an incident spans it: an incident keeps the pre-roll
before it and the post-roll after it, so a day-long run leaves clips of the moments that
mattered rather than a hundred gigabytes of clean stream.

**Bounded.** Every recorder shares one byte count of the recordings folder against
`rba_recordings_quota_gb`. A recorder that would cross it stops and states why — in its summary
and in the archive's README — rather than filling the analyzer host's disk.

No key ever reaches a file: decryption goes through the run's `DrmContext`, which holds the key
in memory for the life of the job.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import shutil
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.drm.context import DrmContext
from app.hls.playlist import MediaPlaylist
from app.net.fetcher import Fetcher

logger = logging.getLogger(__name__)

FULL = "full"
INCIDENTS = "incidents"

CACHE_ENTRIES = 64
PRUNE_EVERY_S = 10.0
DRAIN_TIMEOUT_S = 60.0

INDEX = "index.jsonl"
SUMMARY = "recording.json"


def _slug(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_.@" else "_" for ch in text)
    return cleaned.strip("_")[:80] or "variant"


def _extension(uri: str, default: str = "ts") -> str:
    tail = uri.split("?", 1)[0].rsplit("/", 1)[-1]
    if "." in tail:
        suffix = tail.rsplit(".", 1)[-1].lower()
        if suffix.isalnum() and len(suffix) <= 4:
            return suffix
    return default


# -- the shared quota --------------------------------------------------------


class _Usage:
    """Bytes held under the recordings folder, measured once and counted from then on."""

    def __init__(self) -> None:
        self.root: Path | None = None
        self.bytes = 0

    def ensure(self, root: Path) -> None:
        if self.root == root:
            return
        self.root = root
        self.bytes = (
            sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) if root.exists() else 0
        )


USAGE = _Usage()


@dataclass(slots=True)
class RecordedSegment:
    layer: str
    variant: str
    kind: str
    msn: int
    uri: str
    at: str
    duration: float | None
    file: str
    bytes: int
    status: int
    init: str | None = None
    decrypted_file: str | None = None
    drm_reason: str = ""
    error: str | None = None

    def moment(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.at)


@dataclass(slots=True)
class _Job:
    layer: str
    variant: str
    kind: str
    msn: int
    uri: str
    duration: float | None
    byterange: str | None
    map_uri: str | None
    encrypted: bool


@dataclass
class StreamRecorder:
    job_id: str
    root: Path
    fetcher: Fetcher
    mode: str = FULL
    preroll_s: float = 120.0
    postroll_s: float = 60.0
    quota_bytes: int = 200 * 1024**3
    drm: Callable[[], DrmContext | None] = field(default=lambda: None)

    entries: list[RecordedSegment] = field(default_factory=list)
    deleted: int = 0
    failed: int = 0
    stopped_reason: str = ""
    windows: list[tuple[str | None, dt.datetime, dt.datetime | None]] = field(default_factory=list)
    started_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    _queues: dict[tuple[str, str], asyncio.Queue[_Job]] = field(default_factory=dict)
    _workers: dict[tuple[str, str], asyncio.Task[None]] = field(default_factory=dict)
    _seen: dict[tuple[str, str], set[int]] = field(default_factory=dict)
    _cache: OrderedDict[tuple[str, int], bytes] = field(default_factory=OrderedDict)
    _inits: dict[tuple[str, str, str], tuple[str | None, bytes]] = field(default_factory=dict)
    _last_prune: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    _closed: bool = False

    @property
    def directory(self) -> Path:
        return self.root / self.job_id

    # -- feeding -------------------------------------------------------------

    def offer(self, variant: str, msn: int, body: bytes) -> None:
        """A segment the sampler already fetched, so the recorder need not fetch it again."""
        if not body or self._closed:
            return
        self._cache[(variant, msn)] = body
        while len(self._cache) > CACHE_ENTRIES:
            self._cache.popitem(last=False)

    def observe_playlist(
        self,
        *,
        layer: str,
        variant: str,
        kind: str,
        playlist: MediaPlaylist,
        encrypted: bool,
    ) -> None:
        """Queue every segment this playlist lists that is not recorded yet. Never blocks."""
        if self._closed or self.stopped_reason:
            return
        key = (layer, variant)
        seen = self._seen.setdefault(key, set())
        queue = self._queues.get(key)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[key] = queue
            self._workers[key] = asyncio.create_task(
                self._work(queue), name=f"record:{layer}:{variant}"
            )
        for segment in playlist.segments:
            if segment.msn in seen:
                continue
            seen.add(segment.msn)
            queue.put_nowait(
                _Job(
                    layer=layer,
                    variant=variant,
                    kind=kind,
                    msn=segment.msn,
                    uri=segment.resolved_uri,
                    duration=segment.duration,
                    byterange=segment.byterange,
                    map_uri=segment.map_uri or playlist.map_uri,
                    encrypted=encrypted or playlist.is_encrypted,
                )
            )

    def set_windows(
        self, windows: list[tuple[str | None, dt.datetime, dt.datetime | None]]
    ) -> None:
        """The incidents so far: (rendition or None for the ladder, start, end or None if open)."""
        self.windows = list(windows)

    # -- the worker ----------------------------------------------------------

    async def _work(self, queue: asyncio.Queue[_Job]) -> None:
        while True:
            job = await queue.get()
            try:
                await self._record(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failed += 1
                logger.exception("recording %s MSN %d failed", job.variant, job.msn)
            finally:
                queue.task_done()

    async def _init(self, job: _Job) -> tuple[str | None, bytes]:
        if not job.map_uri:
            return None, b""
        key = (job.layer, job.variant, job.map_uri)
        if key in self._inits:
            return self._inits[key]
        result = await self.fetcher.fetch(job.map_uri)
        if not result.ok:
            self._inits[key] = (None, b"")
            return None, b""
        digest = hashlib.sha256(result.body).hexdigest()[:12]
        extension = _extension(job.map_uri, "mp4")
        relative = f"{_slug(job.layer)}/{_slug(job.variant)}/init_{digest}.{extension}"
        await self._write(relative, result.body)
        self._inits[key] = (relative, result.body)
        return relative, result.body

    async def _record(self, job: _Job) -> None:
        if self.stopped_reason:
            return
        init_file, _ = await self._init(job)
        body = self._cache.pop((job.variant, job.msn), b"")
        status, error = 200, None
        if not body:
            if job.byterange:
                length_text, _, offset_text = job.byterange.partition("@")
                start = int(offset_text) if offset_text.strip().isdigit() else 0
                length = int(length_text) if length_text.strip().isdigit() else 0
                result = await self.fetcher.fetch_range(job.uri, start=start, length=length)
            else:
                result = await self.fetcher.fetch(job.uri)
            status, error = result.status, result.error
            body = result.body if result.ok else b""

        directory = f"{_slug(job.layer)}/{_slug(job.variant)}"
        extension = _extension(job.uri, "m4s" if job.map_uri else "ts")
        relative = f"{directory}/{job.msn:010d}.{extension}"
        if body and not await self._write(relative, body):
            return

        decrypted_file: str | None = None
        drm_reason = ""
        if body and job.encrypted:
            drm = self.drm()
            if drm is None:
                drm_reason = "This run holds no DRM context, so the segment is kept as served."
            else:
                outcome = drm.decrypt(job.variant, body)
                if outcome.ok:
                    decrypted_file = f"{directory}/decrypted/{job.msn:010d}.{extension}"
                    if not await self._write(decrypted_file, outcome.data):
                        decrypted_file = None
                else:
                    drm_reason = outcome.reason

        entry = RecordedSegment(
            layer=job.layer,
            variant=job.variant,
            kind=job.kind,
            msn=job.msn,
            uri=job.uri,
            at=dt.datetime.now(dt.UTC).isoformat(),
            duration=job.duration,
            file=relative if body else "",
            bytes=len(body),
            status=status,
            init=init_file,
            decrypted_file=decrypted_file,
            drm_reason=drm_reason,
            error=error if not body else None,
        )
        if not body:
            self.failed += 1
        self.entries.append(entry)
        await asyncio.to_thread(self._append_index, entry)
        await self.prune()

    async def _write(self, relative: str, data: bytes) -> bool:
        USAGE.ensure(self.root)
        if USAGE.bytes + len(data) > self.quota_bytes:
            self.stopped_reason = (
                f"The recordings folder reached its quota of "
                f"{self.quota_bytes / 1024**3:.0f} GB (rba_recordings_quota_gb), so recording "
                "stopped there. Older recordings are removed by the retention purge."
            )
            logger.warning("recording %s stopped: quota reached", self.job_id)
            return False
        path = self.directory / relative
        await asyncio.to_thread(_write_file, path, data)
        USAGE.bytes += len(data)
        return True

    def _append_index(self, entry: RecordedSegment) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / INDEX).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry)) + "\n")

    # -- incident clips ------------------------------------------------------

    def _kept(self, entry: RecordedSegment, now: dt.datetime) -> bool:
        if self.mode != INCIDENTS:
            return True
        at = entry.moment()
        if (now - at).total_seconds() < self.preroll_s:
            return True
        pre = dt.timedelta(seconds=self.preroll_s)
        post = dt.timedelta(seconds=self.postroll_s)
        for variant, start, end in self.windows:
            if variant is not None and variant != entry.variant:
                continue
            if start - pre <= at and (end is None or at <= end + post):
                return True
        return False

    async def prune(self, *, force: bool = False, now: dt.datetime | None = None) -> int:
        """Delete what no incident needs. Incident mode only; a no-op for a full recording."""
        if self.mode != INCIDENTS:
            return 0
        moment = now or dt.datetime.now(dt.UTC)
        if not force and (moment - self._last_prune).total_seconds() < PRUNE_EVERY_S:
            return 0
        self._last_prune = moment
        kept: list[RecordedSegment] = []
        dropped: list[RecordedSegment] = []
        for entry in self.entries:
            (kept if self._kept(entry, moment) else dropped).append(entry)
        if not dropped:
            return 0
        self.entries = kept
        freed = await asyncio.to_thread(self._delete, dropped)
        USAGE.bytes = max(0, USAGE.bytes - freed)
        self.deleted += len(dropped)
        return len(dropped)

    def _delete(self, entries: list[RecordedSegment]) -> int:
        freed = 0
        for entry in entries:
            for relative in (entry.file, entry.decrypted_file):
                if not relative:
                    continue
                path = self.directory / relative
                with contextlib.suppress(FileNotFoundError):
                    freed += path.stat().st_size
                    path.unlink()
        return freed

    # -- the end -------------------------------------------------------------

    async def close(self) -> dict[str, Any]:
        """Finish what is queued, apply the last prune and write the summary."""
        self._closed = True
        queues = list(self._queues.values())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(queue.join() for queue in queues)), timeout=DRAIN_TIMEOUT_S
            )
        for task in self._workers.values():
            task.cancel()
        for task in self._workers.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.mode == INCIDENTS:
            # Past the run, nothing is "still within the pre-roll": only incidents keep media.
            far = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=self.preroll_s + 1)
            await self.prune(force=True, now=far)
        summary = self.summary()
        await asyncio.to_thread(self._write_summary, summary)
        return summary

    def summary(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "mode": self.mode,
            "started_at": self.started_at.isoformat(),
            "ended_at": dt.datetime.now(dt.UTC).isoformat(),
            "preroll_s": self.preroll_s,
            "postroll_s": self.postroll_s,
            "segments": [asdict(entry) for entry in self.entries],
            "windows": [
                {
                    "variant": variant,
                    "start": start.isoformat(),
                    "end": end.isoformat() if end else None,
                }
                for variant, start, end in self.windows
            ],
            "deleted": self.deleted,
            "failed": self.failed,
            "stopped_reason": self.stopped_reason,
        }

    def _write_summary(self, summary: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / SUMMARY).write_text(json.dumps(summary, indent=1), encoding="utf-8")


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# -- reading back ------------------------------------------------------------


def read_summary(root: Path, job_id: str) -> dict[str, Any] | None:
    """The recording of a job: its summary, or — for a run that died — its index."""
    directory = root / job_id
    summary = directory / SUMMARY
    if summary.exists():
        loaded: dict[str, Any] = json.loads(summary.read_text(encoding="utf-8"))
        return loaded
    index = directory / INDEX
    if not index.exists():
        return None
    segments = []
    for line in index.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            segments.append(json.loads(line))
    present = [s for s in segments if s.get("file") and (directory / s["file"]).exists()]
    return {
        "job_id": job_id,
        "mode": "unknown",
        "segments": present,
        "windows": [],
        "stopped_reason": "The run ended without writing its summary; the index is read instead.",
    }


def purge(root: Path, *, cutoff: dt.datetime, pinned_cutoff: dt.datetime, running: set[str]) -> int:
    """Remove recordings past retention: a full recording follows the sample retention, an
    incident-clip recording the pinned retention. A running job's folder is never touched."""
    if not root.exists():
        return 0
    removed = 0
    for directory in root.iterdir():
        if not directory.is_dir() or directory.name in running:
            continue
        summary = read_summary(root, directory.name) or {}
        limit = pinned_cutoff if summary.get("mode") == INCIDENTS else cutoff
        modified = dt.datetime.fromtimestamp(directory.stat().st_mtime, dt.UTC)
        if modified < limit:
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
    if removed:
        USAGE.root = None  # measured again on the next write
    return removed

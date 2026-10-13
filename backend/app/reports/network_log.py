"""The network log of a run: every playlist poll, as files, as a HAR and as a table.

Read from `manifest_polls` / `manifest_bodies` (every playlist fetch, written as the run went)
and `samples_segment` (every segment fetch), so it is complete for the whole run and survives
a restart — unlike the in-memory history a poller keeps for its own comparisons.

The archive:

    polls/<layer>/<variant>/<seq>_<UTC time>.m3u8   every poll, the body exactly as served
                                                    (.txt for a poll that was not a 2xx)
    polls.csv                                       one row per poll, in poll order
    network.har                                     HAR 1.2: opens in Chrome DevTools, Charles
                                                    and Fiddler; playlists and segments
    README.txt                                      what is here and how it was measured

A long aging run polls well over a hundred thousand times, so the archive is written to a
spooled temporary file — in memory up to a bound, on disk past it — rows are read in pages and
bodies are fetched once per distinct content. Nothing is left behind: the file is deleted when
the download has been streamed.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import shlex
import shutil
import tempfile
import zipfile
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterator
from typing import IO, Any
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import func, select

from app.db import session as db_session
from app.db.models import Job as JobRow
from app.db.models import ManifestBody, ManifestPoll, SegmentSample
from app.jobs.manifest_log import decompress

PAGE = 2000
SPOOL_BYTES = 64 * 1024 * 1024
STREAM_CHUNK = 256 * 1024
TOOL_NAME = "StreamLens"


def _iso(moment: dt.datetime | None) -> str:
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _stamp(moment: dt.datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%S.%f")[:-3] + "Z"


def _slug(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in text)
    return cleaned.strip("_")[:80] or "variant"


# -- reading -----------------------------------------------------------------


def poll_payload(row: ManifestPoll) -> dict[str, Any]:
    """One poll as the API serves it, without its body."""
    return {
        "seq": row.seq,
        "at": _iso(row.ts),
        "layer": row.stream_layer,
        "variant": row.variant,
        "kind": row.kind,
        "url": row.url,
        "final_url": row.final_url,
        "status": row.status,
        "error": row.error,
        "ttfb_ms": row.ttfb_ms,
        "total_ms": row.total_ms,
        "bytes": row.bytes,
        "msn": row.msn,
        "last_msn": row.last_msn,
        "headers": row.headers or {},
        "hops": row.hops or [],
        "timings": row.timings or {},
        "body_hash": row.body_hash,
        "pinned": bool(row.pinned),
    }


async def job_user_agent(job_id: str) -> str:
    async with db_session.session_scope() as session:
        row = await session.get(JobRow, job_id)
    params = (row.params if row is not None else None) or {}
    return str(params.get("user_agent") or "")


async def read_body(job_id: str, digest: str) -> str:
    """A stored body, as text. Empty when the poll carried none."""
    if not digest:
        return ""
    async with db_session.session_scope() as session:
        blob = await session.scalar(
            select(ManifestBody.body).where(
                ManifestBody.job_id == job_id, ManifestBody.body_hash == digest
            )
        )
    return decompress(blob).decode("utf-8", errors="replace")


async def poll_count(job_id: str) -> int:
    async with db_session.session_scope() as session:
        value = await session.scalar(
            select(func.count(ManifestPoll.id)).where(ManifestPoll.job_id == job_id)
        )
    return int(value or 0)


async def list_polls(
    job_id: str,
    *,
    variant: str | None = None,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    limit: int = 500,
    after_seq: int = 0,
) -> list[dict[str, Any]]:
    """Polls in poll order, optionally for one rendition and a time range."""
    query = select(ManifestPoll).where(ManifestPoll.job_id == job_id, ManifestPoll.seq > after_seq)
    if variant:
        query = query.where(ManifestPoll.variant == variant)
    if start is not None:
        query = query.where(ManifestPoll.ts >= start)
    if end is not None:
        query = query.where(ManifestPoll.ts <= end)
    # Ordered on the key the (job_id, seq) index serves; the rows carry JSON, so the sort
    # covers the key alone and the rows are fetched by it (see app/db/paging.py).
    async with db_session.session_scope() as session:
        ids = (
            (
                await session.execute(
                    query.with_only_columns(ManifestPoll.id).order_by(ManifestPoll.seq).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            return []
        rows = (
            (await session.execute(select(ManifestPoll).where(ManifestPoll.id.in_(ids))))
            .scalars()
            .all()
        )
    rows = sorted(rows, key=lambda r: r.seq)
    return [poll_payload(row) for row in rows]


async def poll_with_context(
    job_id: str,
    *,
    seq: int | None = None,
    variant: str | None = None,
    at: dt.datetime | None = None,
) -> dict[str, Any] | None:
    """One poll with its body and the previous poll of the same rendition.

    Named by `seq` when a finding carries it; otherwise the poll of `variant` closest at or
    before `at` — the playlist the player had when the moment came.
    """
    async with db_session.session_scope() as session:
        row: ManifestPoll | None = None
        if seq is not None:
            row = await session.scalar(
                select(ManifestPoll).where(ManifestPoll.job_id == job_id, ManifestPoll.seq == seq)
            )
        if row is None and variant:
            base = select(ManifestPoll).where(
                ManifestPoll.job_id == job_id, ManifestPoll.variant == variant
            )
            if at is not None:
                row = await session.scalar(
                    base.where(ManifestPoll.ts <= at).order_by(ManifestPoll.ts.desc()).limit(1)
                )
                if row is None:
                    row = await session.scalar(base.order_by(ManifestPoll.ts).limit(1))
            else:
                row = await session.scalar(base.order_by(ManifestPoll.ts.desc()).limit(1))
        if row is None:
            return None
        previous = await session.scalar(
            select(ManifestPoll)
            .where(
                ManifestPoll.job_id == job_id,
                ManifestPoll.variant == row.variant,
                ManifestPoll.stream_layer == row.stream_layer,
                ManifestPoll.seq < row.seq,
            )
            .order_by(ManifestPoll.seq.desc())
            .limit(1)
        )
    payload = poll_payload(row)
    payload["raw"] = await read_body(job_id, row.body_hash)
    if previous is not None:
        payload["previous"] = {
            **poll_payload(previous),
            "raw": await read_body(job_id, previous.body_hash),
        }
    else:
        payload["previous"] = None
    return payload


# -- cURL --------------------------------------------------------------------


def curl_command(url: str, user_agent: str = "") -> str:
    """The request exactly as the analyzer sent it, for a CDN or packager to reproduce.

    `-k` because the analyzer does not verify TLS (it reports the chain instead), `-L` off on
    purpose: the analyzer follows redirects itself and records each hop, and `-D -` prints the
    headers each hop answered with.
    """
    parts = ["curl", "-sS", "-k", "-D", "-", "-o", "/dev/null"]
    if user_agent:
        parts += ["-A", user_agent]
    parts.append(url)
    return " ".join(shlex.quote(part) for part in parts)


# -- HAR ---------------------------------------------------------------------


def _har_headers(headers: dict[str, Any]) -> list[dict[str, str]]:
    return [{"name": str(k), "value": str(v)} for k, v in (headers or {}).items()]


def _har_query(url: str) -> list[dict[str, str]]:
    return [
        {"name": k, "value": v} for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)
    ]


def _ms(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return -1.0
    return number if number >= 0 else -1.0


def har_poll_entries(poll: dict[str, Any], body: str, user_agent: str) -> Iterator[dict[str, Any]]:
    """A poll as HAR entries: one per redirect hop, the body on the last."""
    hops = poll.get("hops") or []
    if not hops:
        hops = [
            {
                "url": poll["url"],
                "status": poll["status"],
                "location": None,
                "headers": poll.get("headers") or {},
                "timings": poll.get("timings") or {},
            }
        ]
    request_headers = [{"name": "User-Agent", "value": user_agent}] if user_agent else []
    for index, hop in enumerate(hops):
        last = index == len(hops) - 1
        timings = hop.get("timings") or {}
        total = _ms(timings.get("total_ms"))
        ttfb = _ms(timings.get("ttfb_ms"))
        headers = hop.get("headers") or {}
        content_type = next(
            (str(v) for k, v in headers.items() if str(k).lower() == "content-type"),
            "application/vnd.apple.mpegurl",
        )
        yield {
            "pageref": "run",
            "startedDateTime": poll["at"],
            "time": max(total, 0.0),
            "request": {
                "method": "GET",
                "url": hop.get("url") or poll["url"],
                "httpVersion": "HTTP/1.1",
                "cookies": [],
                "headers": request_headers,
                "queryString": _har_query(hop.get("url") or poll["url"]),
                "headersSize": -1,
                "bodySize": 0,
            },
            "response": {
                "status": int(hop.get("status") or poll.get("status") or 0),
                "statusText": "",
                "httpVersion": "HTTP/1.1",
                "cookies": [],
                "headers": _har_headers(headers),
                "content": {
                    "size": poll.get("bytes", 0) if last else 0,
                    "mimeType": content_type,
                    **({"text": body} if last and body else {}),
                },
                "redirectURL": str(hop.get("location") or ""),
                "headersSize": -1,
                "bodySize": poll.get("bytes", 0) if last else 0,
            },
            "cache": {},
            "timings": {
                "blocked": -1,
                "dns": _ms(timings.get("dns_ms")),
                "connect": _ms(timings.get("connect_ms")),
                "ssl": _ms(timings.get("tls_ms")),
                "send": 0,
                "wait": max(ttfb, 0.0),
                "receive": max(total - ttfb, 0.0) if total >= 0 and ttfb >= 0 else 0.0,
            },
            "comment": f"{poll['layer']} {poll['variant']} poll #{poll['seq']}"
            + (f" — {poll['error']}" if poll.get("error") and last else ""),
        }


def har_segment_entry(row: SegmentSample, user_agent: str) -> dict[str, Any]:
    download = _ms(row.download_ms)
    ttfb = _ms(row.ttfb_ms)
    return {
        "pageref": "run",
        "startedDateTime": _iso(row.ts),
        "time": max(download, 0.0),
        "request": {
            "method": "GET",
            "url": row.uri,
            "httpVersion": "HTTP/1.1",
            "cookies": [],
            "headers": [{"name": "User-Agent", "value": user_agent}] if user_agent else [],
            "queryString": _har_query(row.uri),
            "headersSize": -1,
            "bodySize": 0,
        },
        "response": {
            "status": int(row.http_status or 0),
            "statusText": "",
            "httpVersion": "HTTP/1.1",
            "cookies": [],
            "headers": [],
            "content": {"size": int(row.bytes or 0), "mimeType": "video/mp2t"},
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": int(row.bytes or 0),
        },
        "cache": {},
        "timings": {
            "blocked": -1,
            "dns": -1,
            "connect": -1,
            "ssl": -1,
            "send": 0,
            "wait": max(ttfb, 0.0),
            "receive": max(download - ttfb, 0.0) if download >= 0 and ttfb >= 0 else 0.0,
        },
        "comment": f"{row.stream_layer} {row.variant} segment MSN {row.msn}",
    }


# -- the archive -------------------------------------------------------------


class _Bodies:
    """Bodies by hash, read once per distinct content and kept in a small LRU."""

    def __init__(self, job_id: str, size: int = 256) -> None:
        self.job_id = job_id
        self.size = size
        self.cache: OrderedDict[str, str] = OrderedDict()

    async def get(self, digest: str) -> str:
        if not digest:
            return ""
        if digest in self.cache:
            self.cache.move_to_end(digest)
            return self.cache[digest]
        text = await read_body(self.job_id, digest)
        self.cache[digest] = text
        if len(self.cache) > self.size:
            self.cache.popitem(last=False)
        return text


async def _poll_pages(job_id: str) -> AsyncIterator[list[ManifestPoll]]:
    after = 0
    while True:
        async with db_session.session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(ManifestPoll)
                        .where(ManifestPoll.job_id == job_id, ManifestPoll.seq > after)
                        .order_by(ManifestPoll.seq)
                        .limit(PAGE)
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            return
        yield rows
        after = rows[-1].seq


async def _segment_pages(job_id: str) -> AsyncIterator[list[SegmentSample]]:
    after = 0
    while True:
        async with db_session.session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(SegmentSample)
                        .where(SegmentSample.job_id == job_id, SegmentSample.id > after)
                        .order_by(SegmentSample.id)
                        .limit(PAGE)
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            return
        yield rows
        after = rows[-1].id


CSV_COLUMNS = (
    "seq",
    "at_utc",
    "layer",
    "variant",
    "kind",
    "status",
    "error",
    "ttfb_ms",
    "total_ms",
    "bytes",
    "msn",
    "last_msn",
    "changed",
    "pinned",
    "url",
    "final_url",
    "body_hash",
    "file",
)


def _readme(*, job_id: str, channel: str, user_agent: str, polls: int, segments: int) -> str:
    return "\n".join(
        [
            f"{TOOL_NAME} network log",
            "",
            f"Job        : {job_id}",
            f"Channel    : {channel}",
            f"User-Agent : {user_agent or '(not recorded)'}",
            f"Polls      : {polls}",
            f"Segments   : {segments}",
            "",
            "polls/<layer>/<variant>/<seq>_<UTC time>.m3u8",
            "    Every playlist fetch of the run, the body exactly as it was served. A poll",
            "    that did not answer 2xx is written as .txt with its status and error. <seq>",
            "    is the poll number a finding names in its evidence.",
            "polls.csv",
            "    One row per poll: status, timing, media sequence, whether the body changed",
            "    since the rendition's previous poll, and the file it was written to.",
            "network.har",
            "    HAR 1.2 with every playlist fetch (one entry per redirect hop, the body on",
            "    the last) and every segment fetch. Open it in Chrome DevTools (Network tab,",
            "    import), Charles or Fiddler.",
            "",
            "Segment fetches carry status, size and timing; the segment bytes are in the",
            "evidence bundle and the recording, not here.",
            "",
        ]
    )


async def write_archive(job_id: str, target: IO[bytes]) -> dict[str, int]:
    """Write the network log of a job as a ZIP into `target`."""
    async with db_session.session_scope() as session:
        job = await session.get(JobRow, job_id)
    channel = (job.channel_name if job is not None else "") or ""
    user_agent = await job_user_agent(job_id)
    bodies = _Bodies(job_id)
    previous_hash: dict[tuple[str, str], str] = {}
    polls = 0
    segments = 0

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(CSV_COLUMNS)

        # The HAR is spooled to its own file and copied in at the end: a ZIP takes one entry
        # being written at a time, and the poll files are written as the polls are read.
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES) as har_spool:
            har = io.TextIOWrapper(har_spool, encoding="utf-8")  # type: ignore[arg-type]
            har.write(
                json.dumps(
                    {
                        "log": {
                            "version": "1.2",
                            "creator": {"name": TOOL_NAME, "version": "1.0"},
                            "pages": [
                                {
                                    "id": "run",
                                    "title": f"{channel} ({job_id})",
                                    "startedDateTime": _iso(job.started_at if job else None)
                                    or _iso(dt.datetime.now(dt.UTC)),
                                    "pageTimings": {},
                                }
                            ],
                            "entries": [],
                        }
                    }
                )[:-3]
            )
            first = True
            async for page in _poll_pages(job_id):
                for row in page:
                    poll = poll_payload(row)
                    body = await bodies.get(row.body_hash)
                    ok = 200 <= (row.status or 0) < 300 and not row.error
                    extension = "m3u8" if ok else "txt"
                    name = (
                        f"polls/{_slug(row.stream_layer)}/{_slug(row.variant)}/"
                        f"{row.seq:07d}_{_stamp(row.ts)}.{extension}"
                    )
                    if ok:
                        archive.writestr(name, body)
                    else:
                        archive.writestr(
                            name,
                            f"HTTP {row.status}\nerror: {row.error or ''}\n"
                            f"url: {row.url}\n\n{body}",
                        )
                    key = (row.stream_layer, row.variant)
                    changed = previous_hash.get(key) != row.body_hash
                    previous_hash[key] = row.body_hash
                    writer.writerow(
                        [
                            row.seq,
                            _iso(row.ts),
                            row.stream_layer,
                            row.variant,
                            row.kind,
                            row.status,
                            row.error or "",
                            row.ttfb_ms if row.ttfb_ms is not None else "",
                            row.total_ms,
                            row.bytes,
                            row.msn if row.msn is not None else "",
                            row.last_msn if row.last_msn is not None else "",
                            "yes" if changed else "no",
                            "yes" if row.pinned else "no",
                            row.url,
                            row.final_url or "",
                            row.body_hash,
                            name,
                        ]
                    )
                    for entry in har_poll_entries(poll, body, user_agent):
                        har.write(("" if first else ",") + json.dumps(entry))
                        first = False
                    polls += 1
            async for segment_page in _segment_pages(job_id):
                for segment in segment_page:
                    har.write(
                        ("" if first else ",") + json.dumps(har_segment_entry(segment, user_agent))
                    )
                    first = False
                    segments += 1
            har.write("]}}")
            har.flush()
            har.detach()
            har_spool.seek(0)
            with archive.open("network.har", "w") as har_entry:
                shutil.copyfileobj(har_spool, har_entry, STREAM_CHUNK)

        archive.writestr("polls.csv", csv_buffer.getvalue())
        archive.writestr(
            "README.txt",
            _readme(
                job_id=job_id,
                channel=channel,
                user_agent=user_agent,
                polls=polls,
                segments=segments,
            ),
        )
    return {"polls": polls, "segments": segments}


async def stream_archive(job_id: str) -> AsyncIterator[bytes]:
    """The archive as chunks, from a spooled file that is gone once the last chunk is sent."""
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES) as spool:
        await write_archive(job_id, spool)  # type: ignore[arg-type]
        spool.seek(0)
        while True:
            chunk = spool.read(STREAM_CHUNK)
            if not chunk:
                break
            yield chunk

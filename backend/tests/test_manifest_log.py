"""Every playlist poll of a run is written down, and the network log is built from it.

A finding used to be opened against the playlist as it stood *now*: the poller kept its last
400 polls in memory and nothing wrote a poll to the database, so a finding from the third hour
of an aging run could only be shown against a window fetched hours later, and an evidence
download after a restart had no playlists at all. These pin the poll log: every poll of every
rendition numbered and stored, bodies once per distinct content, findings naming the poll they
fired on, and an archive with every poll as a file, a HAR and a CSV.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import zipfile
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.db import session as db_session
from app.db.models import ManifestBody, ManifestPoll
from app.jobs import manifest_log
from app.jobs.manifest_log import ManifestLog, compress, decompress
from app.main import create_app
from app.net.fetcher import FetchResult, Hop, Timings
from app.reports import network_log
from tests.fixtures.server import FixtureServer, build_simple_channel


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


async def _drop(job_id: str) -> None:
    async with db_session.session_scope() as session:
        for model in (ManifestPoll, ManifestBody):
            await session.execute(delete(model).where(model.job_id == job_id))


def _result(body: str, *, status: int = 200, url: str = "https://cdn.example/v/index.m3u8") -> FetchResult:
    raw = body.encode()
    timings = Timings(dns_ms=1.0, connect_ms=2.0, tls_ms=3.0, ttfb_ms=40.0, total_ms=55.0)
    return FetchResult(
        requested_url=url,
        final_url=url,
        status=status,
        headers={"Content-Type": "application/vnd.apple.mpegurl", "Age": "2"},
        body=raw,
        timings=timings,
        hops=[Hop(url=url, status=status, location=None, headers={"Age": "2"}, timings=timings)],
    )


PLAYLIST = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:{msn}\n#EXTINF:6.0,\nseg_{msn}.ts\n"


# -- the writer --------------------------------------------------------------


@pytest.mark.asyncio()
async def test_every_poll_is_a_row_and_a_repeated_body_is_stored_once() -> None:
    job = "poll-log-dedupe"
    await _drop(job)
    log = ManifestLog(job_id=job)
    at = dt.datetime(2026, 10, 13, 9, 0, tzinfo=dt.UTC)
    bodies = [PLAYLIST.format(msn=10), PLAYLIST.format(msn=10), PLAYLIST.format(msn=11)]
    for index, body in enumerate(bodies):
        await log.record(
            seq=log.next_seq(),
            at=at + dt.timedelta(seconds=3 * index),
            layer="PLAYBACK",
            variant="v1",
            kind="video",
            url="https://cdn.example/v/index.m3u8",
            result=_result(body),
            msn=10 + index // 2,
        )
    await log.flush()

    async with db_session.session_scope() as session:
        polls = (
            (await session.execute(select(ManifestPoll).where(ManifestPoll.job_id == job)))
            .scalars()
            .all()
        )
        stored = (
            (await session.execute(select(ManifestBody).where(ManifestBody.job_id == job)))
            .scalars()
            .all()
        )
    try:
        assert sorted(p.seq for p in polls) == [1, 2, 3]
        assert len(stored) == 2, "two distinct bodies, three polls"
        assert {decompress(b.body).decode() for b in stored} == set(bodies)
        first = next(p for p in polls if p.seq == 1)
        assert first.headers["Age"] == "2"
        assert first.hops[0]["status"] == 200
        assert first.timings["ttfb_ms"] == 40.0
    finally:
        await _drop(job)


@pytest.mark.asyncio()
async def test_a_resumed_log_never_reuses_a_number_or_rewrites_a_body() -> None:
    job = "poll-log-resume"
    await _drop(job)
    first = ManifestLog(job_id=job)
    at = dt.datetime(2026, 10, 13, 9, 0, tzinfo=dt.UTC)
    await first.record(
        seq=first.next_seq(), at=at, layer="PLAYBACK", variant="v1", kind="video",
        url="u", result=_result(PLAYLIST.format(msn=1)),
    )
    await first.flush()

    second = ManifestLog(job_id=job)
    second.resume_from(await manifest_log.last_seq(job))
    await second.record(
        seq=second.next_seq(), at=at, layer="PLAYBACK", variant="v1", kind="video",
        url="u", result=_result(PLAYLIST.format(msn=1)),
    )
    await second.flush()
    try:
        assert second.failed_flushes == 0
        async with db_session.session_scope() as session:
            seqs = sorted(
                (await session.execute(select(ManifestPoll.seq).where(ManifestPoll.job_id == job)))
                .scalars()
                .all()
            )
            bodies = await session.scalar(
                select(func.count()).select_from(ManifestBody).where(ManifestBody.job_id == job)
            )
        assert seqs == [1, 2]
        assert bodies == 1
    finally:
        await _drop(job)


def test_compression_round_trips() -> None:
    body = (PLAYLIST.format(msn=7) * 50).encode()
    assert decompress(compress(body)) == body
    assert len(compress(body)) < len(body) / 5


# -- retention ---------------------------------------------------------------


@pytest.mark.asyncio()
async def test_the_purge_keeps_pinned_polls_and_running_jobs_and_drops_orphan_bodies() -> None:
    old = dt.datetime.now(dt.UTC) - dt.timedelta(days=60)
    jobs = ("purge-done", "purge-running")
    for job in jobs:
        await _drop(job)
    for job in jobs:
        log = ManifestLog(job_id=job)
        for index, msn in enumerate((1, 2)):
            await log.record(
                seq=log.next_seq(), at=old + dt.timedelta(seconds=index), layer="PLAYBACK",
                variant="v1", kind="video", url="u", result=_result(PLAYLIST.format(msn=msn)),
            )
        await log.flush()
    await manifest_log.pin_polls("purge-done", seqs={2}, windows=[])

    try:
        await manifest_log.purge(
            cutoff=dt.datetime.now(dt.UTC) - dt.timedelta(days=30),
            pinned_cutoff=dt.datetime.now(dt.UTC) - dt.timedelta(days=180),
            running={"purge-running"},
        )
        async with db_session.session_scope() as session:
            done = (
                (await session.execute(select(ManifestPoll.seq).where(ManifestPoll.job_id == "purge-done")))
                .scalars()
                .all()
            )
            running = await session.scalar(
                select(func.count()).select_from(ManifestPoll).where(ManifestPoll.job_id == "purge-running")
            )
            done_bodies = await session.scalar(
                select(func.count()).select_from(ManifestBody).where(ManifestBody.job_id == "purge-done")
            )
        assert list(done) == [2], "the unpinned poll is gone, the pinned one kept"
        assert running == 2, "a running job is never purged"
        assert done_bodies == 1, "the body only the purged poll carried went with it"

        await manifest_log.purge(
            cutoff=dt.datetime.now(dt.UTC) - dt.timedelta(days=30),
            pinned_cutoff=dt.datetime.now(dt.UTC),
            running=set(),
        )
        async with db_session.session_scope() as session:
            left = await session.scalar(
                select(func.count()).select_from(ManifestPoll).where(ManifestPoll.job_id.in_(jobs))
            )
        assert left == 0, "past the pinned retention, a pinned poll goes too"
    finally:
        for job in jobs:
            await _drop(job)


# -- a real run --------------------------------------------------------------


async def _run_job(origin: FixtureServer, *, remove: str | None = None) -> Any:
    from app.api.schemas import JobOptionsIn
    from app.jobs.manager import job_manager

    url = build_simple_channel(origin, variant_count=2, segment_count=6)
    if remove:
        origin.remove(remove)
    await job_manager.start()
    handle = await job_manager.submit(
        job_type="realtime",
        playback_url=url,
        channel_name="Poll log channel",
        options=JobOptionsIn().to_session_options(duration_s=5.0, default_record=False),
    )
    assert handle.task is not None
    await handle.task
    return handle


@pytest.mark.asyncio()
async def test_a_realtime_run_logs_every_poll_and_its_findings_name_them(
    origin: FixtureServer,
) -> None:
    handle = await _run_job(origin, remove="low-seg2.ts")
    try:
        assert handle.poll_log is not None and handle.poll_log.failed_flushes == 0
        async with db_session.session_scope() as session:
            polls = (
                (await session.execute(select(ManifestPoll).where(ManifestPoll.job_id == handle.id)))
                .scalars()
                .all()
            )
        assert polls, "a realtime run with no evidence option still logs its polls"
        kinds = {p.kind for p in polls}
        assert "master" in kinds and "video" in kinds
        seqs = [p.seq for p in polls]
        assert len(seqs) == len(set(seqs)), "every poll has its own number"
        by_seq = {p.seq: p for p in polls}

        named = manifest_log.poll_seqs_of(handle.result.findings)
        assert named, "findings raised while a poll was handled name it"
        assert named <= set(by_seq), "every poll a finding names is in the log"

        missing = next(f for f in handle.result.findings if f.rule.id == "HTTP-005")
        poll = missing.evidence[0]["poll"]
        assert poll["variant"] == missing.variant, "a segment finding names its own rendition's poll"
        assert by_seq[poll["seq"]].pinned, "the poll a finding fired on is pinned"

        served = await network_log.poll_with_context(handle.id, seq=poll["seq"])
        assert served is not None
        assert "#EXTM3U" in served["raw"]
        assert served["variant"] == missing.variant
    finally:
        await _drop(handle.id)


@pytest.mark.asyncio()
async def test_the_network_log_holds_every_poll_as_a_file_a_har_and_a_csv(
    origin: FixtureServer,
) -> None:
    handle = await _run_job(origin)
    try:
        total = await network_log.poll_count(handle.id)
        buffer = io.BytesIO()
        counts = await network_log.write_archive(handle.id, buffer)
        assert counts["polls"] == total > 0

        archive = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
        names = archive.namelist()
        files = [n for n in names if n.startswith("polls/")]
        assert len(files) == total, "one file per poll, none dropped"
        assert any("/master/" in n for n in files)
        assert "README.txt" in names

        rows = list(csv.DictReader(io.StringIO(archive.read("polls.csv").decode())))
        assert len(rows) == total
        assert [int(r["seq"]) for r in rows] == sorted(int(r["seq"]) for r in rows)
        assert set(rows[0]) >= {"seq", "at_utc", "variant", "status", "changed", "file"}

        har = json.loads(archive.read("network.har"))
        entries = har["log"]["entries"]
        assert har["log"]["version"] == "1.2"
        assert len(entries) >= total
        playlist = next(e for e in entries if "#EXTM3U" in e["response"]["content"].get("text", ""))
        assert playlist["response"]["status"] == 200
        assert any(e["request"]["url"].endswith(".ts") for e in entries), "segment fetches too"
    finally:
        await _drop(handle.id)


@pytest.mark.asyncio()
async def test_the_api_serves_a_poll_with_its_previous_one_and_a_curl(
    origin: FixtureServer,
) -> None:
    from app.api import reports as reports_api

    handle = await _run_job(origin)
    try:
        listed = await reports_api.job_polls(handle.id, variant=None, start=None, end=None, after_seq=0, limit=500)
        video = [p for p in listed["polls"] if p["kind"] == "video"]
        later = video[-1]
        served = await reports_api.job_poll(handle.id, later["seq"])
        assert served["raw"].startswith("#EXTM3U")
        assert served["previous"] is None or served["previous"]["seq"] < served["seq"]
        assert served["curl"].startswith("curl ") and later["url"] in served["curl"]

        at = await reports_api.job_poll_at(handle.id, variant=later["variant"], at=later["at"])
        assert at["seq"] == later["seq"]

        response = await reports_api.network_log_archive(handle.id)
        body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[misc]
        assert zipfile.ZipFile(io.BytesIO(body)).testzip() is None
    finally:
        await _drop(handle.id)


@pytest.mark.asyncio()
async def test_a_run_with_no_log_says_so() -> None:
    from fastapi import HTTPException

    from app.api import reports as reports_api

    with pytest.raises(HTTPException) as raised:
        await reports_api.network_log_archive("no-such-job")
    assert raised.value.status_code == 404

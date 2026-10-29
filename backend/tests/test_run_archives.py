"""A finished run's files land in object storage, one folder per channel, and are served from there.

Local storage is the development path and S3 behind CloudFront the production one; both are
exercised. S3 runs against moto, so nothing reaches AWS.
"""

from __future__ import annotations

import io
import os
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db import session as db_session
from app.db.models import Job, Report
from app.main import create_app
from app.storage import get_storage, reset_storage
from tests.fixtures.server import FixtureServer, build_simple_channel


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture()
def local_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    settings = get_settings()
    monkeypatch.setattr(settings, "rba_storage_backend", "local")
    monkeypatch.setattr(settings, "rba_storage_dir", tmp_path / "store")
    monkeypatch.setattr(settings, "rba_recordings_dir", tmp_path / "recordings")
    reset_storage()
    yield tmp_path / "store"
    reset_storage()


async def _recorded_run(origin: FixtureServer, name: str = "Archive Channel") -> str:
    from app.api.schemas import JobOptionsIn
    from app.jobs.manager import job_manager

    url = build_simple_channel(origin, variant_count=1, segment_count=4)
    await job_manager.start()
    handle = await job_manager.submit(
        job_type="realtime",
        playback_url=url,
        channel_name=name,
        options=JobOptionsIn(record_stream=True).to_session_options(
            duration_s=4.0, default_record=False
        ),
    )
    assert handle.task is not None
    await handle.task
    await job_manager.drain_archives()
    return handle.id


async def _prefix(job_id: str) -> str:
    async with db_session.session_scope() as session:
        row = await session.get(Job, job_id)
        assert row is not None and row.storage_prefix
        return row.storage_prefix


async def test_a_finished_run_files_its_archives_under_its_channel_folder(
    origin: FixtureServer, local_store: Path
) -> None:
    from app.api.reports import job_recording, job_recording_archive, network_log_archive

    job_id = await _recorded_run(origin)
    prefix = await _prefix(job_id)
    keys = {o.key for o in await get_storage().list_keys(prefix)}

    assert prefix.startswith("channels/archive-channel-")
    assert f"{prefix}/archives/network-log.zip" in keys
    assert f"{prefix}/archives/evidence.zip" in keys
    assert f"{prefix}/recording/full/recording.zip" in keys
    assert f"{prefix}/recording/full/index.jsonl" in keys
    # The recording moved: the analyzer host keeps no copy once the run has ended.
    assert not (get_settings().rba_recordings_dir / job_id).exists()

    state = await job_recording(job_id)
    assert state["available"] and state["stored"] and state["mode"] == "full"

    response = await network_log_archive(job_id)
    assert isinstance(response, StreamingResponse)
    body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[misc]
    assert zipfile.ZipFile(io.BytesIO(body)).testzip() is None
    assert isinstance(await job_recording_archive(job_id), StreamingResponse)


async def test_a_run_replays_from_a_recording_that_moved_to_storage(
    origin: FixtureServer, local_store: Path
) -> None:
    from app.api.replay import replay_job
    from app.jobs.manager import job_manager

    job_id = await _recorded_run(origin, name="Replay From Storage")
    for path in list(origin.routes):
        origin.remove(path)

    started = await replay_job(job_id)
    handle = job_manager.get(started["job_id"])
    assert handle is not None and handle.task is not None
    result = await handle.task

    assert started["recorded"] is True
    assert result.replay["gaps"] == 0, result.replay["gap_examples"]
    assert result.replay["segments_timed"] + result.replay["segments_untimed"] > 0


async def test_deleting_a_run_removes_its_storage_folder(
    origin: FixtureServer, local_store: Path
) -> None:
    from app.jobs import purge

    job_id = await _recorded_run(origin, name="Deleted Channel")
    prefix = await _prefix(job_id)
    assert await get_storage().list_keys(prefix)

    purged = await purge.purge_job(job_id)

    assert purged is not None and purged.files > 0
    assert await get_storage().list_keys(prefix) == []


@pytest.fixture()
def s3_cdn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from moto import mock_aws

    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    settings = get_settings()
    with mock_aws():
        import boto3

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="streamlens-prod")
        monkeypatch.setattr(settings, "rba_storage_backend", "s3")
        monkeypatch.setattr(settings, "rba_s3_bucket", "streamlens-prod")
        monkeypatch.setattr(settings, "rba_s3_region", "us-east-1")
        monkeypatch.setattr(settings, "rba_cdn_base_url", "https://d222.cloudfront.net")
        monkeypatch.setattr(settings, "rba_report_storage", "storage")
        monkeypatch.setattr(settings, "rba_recordings_dir", tmp_path / "recordings")
        reset_storage()
        yield
        reset_storage()


async def test_on_s3_downloads_go_through_cloudfront(origin: FixtureServer, s3_cdn: None) -> None:
    from app.api.reports import download_report, evidence_bundle, network_log_archive
    from app.jobs.manager import job_manager
    from app.reports import service

    job_id = await _recorded_run(origin, name="Cdn Channel")
    prefix = await _prefix(job_id)

    response = await network_log_archive(job_id)
    assert isinstance(response, RedirectResponse)
    assert response.headers["location"] == (
        f"https://d222.cloudfront.net/{prefix}/archives/network-log.zip"
    )
    assert isinstance(await evidence_bundle(job_id), RedirectResponse)

    handle = job_manager.get(job_id)
    assert handle is not None and handle.result is not None
    await service.generate(handle, fmt="html")
    async with db_session.session_scope() as session:
        report = (
            await session.execute(
                select(Report.id, Report.content, Report.storage_key).where(Report.job_id == job_id)
            )
        ).first()
    assert report is not None
    assert report.content is None, "the bytes are in storage, not in the row"
    assert report.storage_key and report.storage_key.startswith(f"{prefix}/reports/")
    download = await download_report(report.id, inline=False)
    assert isinstance(download, RedirectResponse)
    # Viewed inline, the Reports tab reads it through the backend.
    inline = await download_report(report.id, inline=True)
    assert inline.body.startswith(b"<!")

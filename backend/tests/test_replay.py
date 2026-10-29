"""A finished run can be re-analysed from what it stored, without the network.

The original run polls the fixture origin and records the stream. The origin is then emptied:
every route it served is gone, so a replay that reached the network would read 404s and raise
HTTP findings. The replay must instead serve every playlist from the poll log and every segment
from the recording, raise none of those findings, and state what it was (INFO-008).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.analysis.replay import _range_key
from app.db import session as db_session
from app.db.models import Finding, Job, ManifestBody, ManifestPoll, PlaylistSample, SegmentSample
from app.main import create_app
from tests.fixtures.server import FixtureServer, build_simple_channel


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def test_a_range_header_is_spelled_as_the_playlist_spells_it() -> None:
    assert _range_key({"Range": "bytes=100-199"}) == "100@100"
    assert _range_key({"range": "bytes=0-9"}) == "10@0"
    assert _range_key(None) is None
    assert _range_key({"Range": "bytes=5-"}) is None


@pytest.mark.asyncio()
async def test_a_recorded_run_replays_from_storage_and_says_so(
    origin: FixtureServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.replay import replay_job
    from app.api.schemas import JobOptionsIn
    from app.config import get_settings
    from app.jobs.manager import job_manager

    monkeypatch.setattr(get_settings(), "rba_recordings_dir", tmp_path)
    url = build_simple_channel(origin, variant_count=2, segment_count=6)
    await job_manager.start()
    original = await job_manager.submit(
        job_type="realtime",
        playback_url=url,
        channel_name="Replayed channel",
        options=JobOptionsIn(record_stream=True).to_session_options(
            duration_s=5.0, default_record=False
        ),
    )
    assert original.task is not None
    first = await original.task
    replay_id = ""
    try:
        # The live run is not a replay.
        assert "INFO-008" not in {f.rule.id for f in first.findings}
        assert first.replay == {}

        # Nothing is left on the origin: only stored responses can answer now.
        for path in list(origin.routes):
            origin.remove(path)
        origin.clear_log()

        started = await replay_job(original.id)
        replay_id = started["job_id"]
        assert started["replay_of"] == original.id
        assert started["recorded"] is True
        handle = job_manager.get(replay_id)
        assert handle is not None and handle.task is not None
        result = await handle.task

        ids = {f.rule.id for f in result.findings}
        assert "INFO-008" in ids
        assert not any(rule.startswith("HTTP") for rule in ids), ids
        assert result.replay["of"] == original.id
        assert result.replay["playlists_served"] > 0
        assert result.replay["segments_timed"] + result.replay["segments_untimed"] > 0
        assert result.replay["gaps"] == 0, result.replay["gap_examples"]
        assert result.options["replay_of"] == original.id
        assert handle.parent_job_id == original.id
        assert "not re-measured" in result.replay["statement"]
    finally:
        async with db_session.session_scope() as session:
            for job_id in (original.id, replay_id):
                if not job_id:
                    continue
                for model in (ManifestPoll, ManifestBody, SegmentSample, PlaylistSample, Finding):
                    await session.execute(delete(model).where(model.job_id == job_id))
                await session.execute(delete(Job).where(Job.id == job_id))


@pytest.mark.asyncio()
async def test_a_run_without_a_poll_log_is_refused() -> None:
    from fastapi import HTTPException

    from app.api.replay import replay_job

    with pytest.raises(HTTPException) as refused:
        await replay_job("no-such-run")
    assert refused.value.status_code == 404

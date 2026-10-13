"""One owner's escalation as one download: text, findings, marked playlists, a cURL per request."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import session as db_session
from app.db.models import ManifestBody, ManifestPoll
from app.main import create_app
from app.reports.escalation_pack import build_pack
from tests.fixtures.server import FixtureServer, build_simple_channel


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


@pytest.mark.asyncio()
async def test_a_cdn_pack_holds_only_the_cdn_issue_its_playlists_and_a_curl(
    origin: FixtureServer,
) -> None:
    from app.api.schemas import JobOptionsIn
    from app.jobs.manager import job_manager

    url = build_simple_channel(origin, variant_count=2, segment_count=6)
    origin.remove("low-seg2.ts")
    origin.remove("mid-seg2.ts")
    await job_manager.start()
    handle = await job_manager.submit(
        job_type="aging",
        playback_url=url,
        channel_name="Pack channel",
        options=JobOptionsIn().to_session_options(duration_s=5.0, default_record=True),
    )
    assert handle.task is not None
    await handle.task
    try:
        for leave_memory in (False, True):
            if leave_memory:
                job_manager.jobs.pop(handle.id)  # rebuilt from the rows after a restart
            data = await build_pack(handle.id, "CDN")
            assert data is not None
            pack = zipfile.ZipFile(io.BytesIO(data))
            names = pack.namelist()
            escalation = pack.read("ESCALATION.txt").decode()
            assert escalation.startswith("Subject: ")
            assert escalation.count("HTTP-005 —") == 1, "one issue, however many renditions"
            findings = json.loads(pack.read("findings.json"))
            assert {f["rule_id"] for f in findings} >= {"HTTP-005"}
            assert all(f["severity"] not in ("PASS", "INFO") for f in findings)
            marked = [n for n in names if n.endswith(".marked.txt") and "HTTP-005" in n]
            assert len(marked) == 2, names
            assert any("seg2.ts" in pack.read(n).decode() for n in marked)
            assert "curl " in pack.read("reproduce.sh").decode()

        assert await build_pack(handle.id, "NOBODY") is None
    finally:
        async with db_session.session_scope() as session:
            for model in (ManifestPoll, ManifestBody):
                await session.execute(delete(model).where(model.job_id == handle.id))

"""Where a report lives.

The default is the database: the bytes go into the reports table and nothing is written to
the analyzer host, so a report survives the container that rendered it and any instance can
serve one it did not produce. `RBA_REPORT_STORAGE=both` mirrors a copy to disk as well.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.config import get_settings
from app.db import session as db_session
from app.db.models import Report
from app.main import create_app
from tests.fixtures.server import FixtureServer, build_simple_channel


def _report_for(client: TestClient, url: str, name: str) -> dict[str, Any]:
    """Run a short session and generate one HTML report from it."""
    session_id = client.post(
        "/api/realtime/sessions", json={"playback_url": url, "channel_name": name}
    ).json()["id"]
    deadline = time.time() + 25
    while time.time() < deadline:
        if client.get(f"/api/realtime/sessions/{session_id}").json()["status"] == "RUNNING":
            break
        time.sleep(0.3)
    client.delete(f"/api/realtime/sessions/{session_id}")
    generated: dict[str, Any] = client.post(
        f"/api/realtime/sessions/{session_id}/report?format=html"
    ).json()
    return generated


def test_a_report_is_stored_in_the_database_and_not_on_disk(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)
    reports_dir = get_settings().rba_reports_dir
    before = set(reports_dir.glob("*")) if reports_dir.exists() else set()

    with TestClient(create_app()) as client:
        generated = _report_for(client, url, "Stored In Database")

        assert generated["path"] is None, "the default storage writes nothing to the host"

        download = client.get(f"/api/reports/{generated['id']}/download")
        assert download.status_code == 200
        assert download.content.startswith(b"<!doctype html>")
        assert len(download.content) == generated["size_bytes"]

    after = set(reports_dir.glob("*")) if reports_dir.exists() else set()
    assert after == before, "no report file appeared on the analyzer host"


def test_the_stored_bytes_are_the_bytes_served(origin: FixtureServer) -> None:
    """What the download returns is what the reports table holds, byte for byte."""
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    with TestClient(create_app()) as client:
        generated = _report_for(client, url, "Byte For Byte")
        served = client.get(f"/api/reports/{generated['id']}/download").content

    async def stored() -> bytes | None:
        async with db_session.session_scope() as session:
            # The blob is deferred on the model, so the read asks for it by name.
            row = (
                (
                    await session.execute(
                        select(Report)
                        .where(Report.id == generated["id"])
                        .options(undefer(Report.content))
                    )
                )
                .scalars()
                .one()
            )
            return row.content

    import asyncio

    assert asyncio.run(stored()) == served


def test_an_inline_download_renders_rather_than_downloads(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    with TestClient(create_app()) as client:
        generated = _report_for(client, url, "Inline Disposition")
        report_id = generated["id"]

        attached = client.get(f"/api/reports/{report_id}/download")
        inline = client.get(f"/api/reports/{report_id}/download?inline=1")

    assert attached.headers["content-disposition"].startswith("attachment")
    assert inline.headers["content-disposition"].startswith("inline")
    assert attached.content == inline.content


def test_a_row_written_by_an_older_deployment_is_still_served(tmp_path: Path) -> None:
    """A row with a path and no content predates database storage; it still resolves."""
    from app.reports import service

    legacy = tmp_path / "legacy-report.html"
    legacy.write_text("<!doctype html><title>legacy</title>", encoding="utf-8")

    import asyncio

    async def store_and_read() -> tuple[bytes, str, str] | None:
        async with db_session.session_scope() as session:
            row = Report(
                job_id="legacy-job",
                channel_name="Legacy Channel",
                job_type="realtime",
                format="html",
                content=None,
                path=str(legacy),
                size_bytes=legacy.stat().st_size,
            )
            session.add(row)
            await session.flush()
            report_id = row.id
        return await service.get_report(report_id)

    with TestClient(create_app()):
        found = asyncio.run(store_and_read())

    assert found is not None
    data, media, _filename = found
    assert data == legacy.read_bytes()
    assert media.startswith("text/html")


@pytest.mark.parametrize(
    ("storage", "on_disk", "in_database"),
    [
        ("database", False, True),
        ("both", True, True),
        ("disk", True, False),
    ],
)
def test_the_storage_setting_decides_where_the_bytes_go(
    storage: str, on_disk: bool, in_database: bool
) -> None:
    settings = get_settings()
    original = settings.rba_report_storage
    try:
        settings.rba_report_storage = storage
        assert settings.reports_on_disk is on_disk
        assert settings.reports_in_database is in_database
    finally:
        settings.rba_report_storage = original

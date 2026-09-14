"""Analysed channels: what a finished analysis leaves behind, and deleting it.

A realtime session is cleared from the Realtime tab the moment the operator stops it, so
these endpoints are the only way back to the run. They read from the database rather than
the job manager, which is what makes a channel analysed before the last restart still
readable.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from tests.fixtures.server import FixtureServer, build_simple_channel


def _wait_for(client: TestClient, path: str, predicate: Any, timeout: float = 25.0) -> Any:
    deadline = time.time() + timeout
    body: Any = None
    while time.time() < deadline:
        response = client.get(path)
        if response.status_code == 200:
            body = response.json()
            if predicate(body):
                return body
        time.sleep(0.3)
    raise AssertionError(f"{path} never satisfied the predicate; last body was {body}")


def _analysed_channel(client: TestClient, url: str, name: str) -> str:
    """Run a realtime session, stop it, and return its id once it is filed."""
    session_id = client.post(
        "/api/realtime/sessions", json={"playback_url": url, "channel_name": name}
    ).json()["id"]
    _wait_for(
        client,
        f"/api/realtime/sessions/{session_id}",
        lambda body: body["status"] in ("RUNNING", "COMPLETED"),
    )
    client.delete(f"/api/realtime/sessions/{session_id}")
    _wait_for(
        client,
        "/api/channels",
        lambda body: any(c["id"] == session_id for c in body["channels"]),
    )
    return str(session_id)


def test_a_stopped_session_is_listed_as_an_analysed_channel(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    with TestClient(create_app()) as client:
        before = {c["id"] for c in client.get("/api/channels").json()["channels"]}

        session_id = _analysed_channel(client, url, "Filed Channel")

        listing = client.get("/api/channels").json()
        assert session_id not in before
        channel = next(c for c in listing["channels"] if c["id"] == session_id)
        assert channel["channel_name"] == "Filed Channel"
        assert channel["type"] == "realtime"
        assert channel["status"] in ("CANCELLED", "COMPLETED")
        assert channel["reports"] == []


def test_an_analysed_channel_carries_its_findings_and_its_reports(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    with TestClient(create_app()) as client:
        session_id = _analysed_channel(client, url, "Reported Channel")
        report = client.post(f"/api/realtime/sessions/{session_id}/report?format=html")
        assert report.status_code == 200

        detail = client.get(f"/api/channels/{session_id}").json()
        assert detail["channel_name"] == "Reported Channel"
        assert "findings" in detail
        assert "incidents" in detail
        assert "vpb" in detail
        assert [r["format"] for r in detail["reports"]] == ["html"]

        listed = next(
            c for c in client.get("/api/channels").json()["channels"] if c["id"] == session_id
        )
        assert len(listed["reports"]) == 1


def test_deleting_a_channel_removes_its_row_and_its_report(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    with TestClient(create_app()) as client:
        session_id = _analysed_channel(client, url, "Doomed Channel")
        report_id = client.post(f"/api/realtime/sessions/{session_id}/report?format=html").json()[
            "id"
        ]
        assert client.get(f"/api/reports/{report_id}/download").status_code == 200

        deleted = client.delete(f"/api/channels/{session_id}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert deleted.json()["reports_removed"] == 1

        # The bytes go with the row, so the download stops resolving.
        assert client.get(f"/api/reports/{report_id}/download").status_code == 404
        # The database is shared across this module's tests, so only this channel is asserted.
        remaining = client.get("/api/channels").json()["channels"]
        assert all(channel["id"] != session_id for channel in remaining)
        assert client.get(f"/api/channels/{session_id}").status_code == 404
        reports = client.get("/api/reports").json()["reports"]
        assert all(report["job_id"] != session_id for report in reports)


def test_deleting_an_unknown_channel_reports_it_rather_than_succeeding() -> None:
    with TestClient(create_app()) as client:
        assert client.delete("/api/channels/does-not-exist").status_code == 404


def test_a_running_analysis_cannot_be_deleted_until_it_is_stopped(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    with TestClient(create_app()) as client:
        session_id = client.post(
            "/api/realtime/sessions", json={"playback_url": url, "channel_name": "Still Running"}
        ).json()["id"]
        _wait_for(
            client,
            f"/api/realtime/sessions/{session_id}",
            lambda body: body["status"] == "RUNNING",
        )

        refused = client.delete(f"/api/channels/{session_id}")
        assert refused.status_code == 409
        assert "running" in refused.json()["detail"].lower()

        client.delete(f"/api/realtime/sessions/{session_id}")

"""Reports, aging jobs, and bulk analysis end to end."""

from __future__ import annotations

import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.bulk import parsers
from app.main import create_app
from tests.fixtures.server import FixtureServer, build_simple_channel


def _wait_for(client: TestClient, path: str, predicate, timeout: float = 90.0) -> dict:  # type: ignore[no-untyped-def]
    deadline = time.time() + timeout
    body: dict = {}
    while time.time() < deadline:
        response = client.get(path)
        if response.status_code == 200:
            body = response.json()
            if predicate(body):
                return body
        time.sleep(0.5)
    raise AssertionError(f"{path} did not reach the expected state in {timeout} s: {body}")


# ---------------------------------------------------------------------------
# Bulk input parsing
# ---------------------------------------------------------------------------


def test_csv_columns_are_matched_through_their_aliases() -> None:
    csv = (
        "Channel,URL,Country,Origin URL\n"
        "News HD,https://cdn/news/master.m3u8,IN,https://origin/news/master.m3u8\n"
    )
    rows, mapping = parsers.parse(csv.encode(), "channels.csv")
    assert set(mapping.values()) >= {"channel_name", "playback_url", "country", "origin_url"}
    assert rows[0].channel_name == "News HD"
    assert rows[0].playback_url == "https://cdn/news/master.m3u8"
    assert rows[0].origin_url == "https://origin/news/master.m3u8"
    assert rows[0].valid is True


def test_a_row_with_a_relative_url_is_reported_as_invalid() -> None:
    csv = "channel_name,playback_url\nBad,/relative/path.m3u8\n"
    rows, _ = parsers.parse(csv.encode(), "channels.csv")
    assert rows[0].valid is False
    assert "playback_url is not an absolute http or https URL" in rows[0].errors


def test_a_row_with_an_empty_channel_name_is_reported_as_invalid() -> None:
    csv = "channel_name,playback_url\n,https://cdn/a.m3u8\n"
    rows, _ = parsers.parse(csv.encode(), "channels.csv")
    assert rows[0].valid is False
    assert "channel_name is empty" in rows[0].errors


def test_a_file_without_the_required_columns_is_rejected_with_the_aliases() -> None:
    with pytest.raises(parsers.BulkParseError) as excinfo:
        parsers.parse(b"foo,bar\n1,2\n", "channels.csv")
    assert "playback_url" in str(excinfo.value)
    assert "hls_url" in str(excinfo.value)


def test_json_accepts_a_list_and_a_wrapped_object() -> None:
    payload = [{"channel_name": "A", "playback_url": "https://cdn/a.m3u8"}]
    rows, _ = parsers.parse(json.dumps(payload).encode(), "channels.json")
    assert rows[0].channel_name == "A"

    wrapped = {"channels": payload}
    rows, _ = parsers.parse(json.dumps(wrapped).encode(), "channels.json")
    assert rows[0].channel_name == "A"


def test_the_xlsx_template_round_trips() -> None:
    rows, mapping = parsers.parse(parsers.template_xlsx(), "template.xlsx")
    assert set(mapping.values()) >= {"channel_name", "playback_url"}
    assert rows[0].channel_name.startswith("Samsung TV Plus")
    assert rows[0].valid is True


def test_the_csv_template_round_trips() -> None:
    rows, _ = parsers.parse(parsers.template_csv().encode(), "template.csv")
    assert rows[0].valid is True


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def test_a_realtime_report_is_self_contained_html(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=2, segment_count=4)

    with TestClient(create_app()) as client:
        session_id = client.post(
            "/api/realtime/sessions", json={"playback_url": url, "channel_name": "Fixture HD"}
        ).json()["id"]
        time.sleep(6.0)

        generated = client.post(f"/api/realtime/sessions/{session_id}/report?format=html")
        assert generated.status_code == 200
        body = generated.json()
        report_id = body["id"]

        download = client.get(f"/api/reports/{report_id}/download")
        client.delete(f"/api/realtime/sessions/{session_id}")

    assert download.status_code == 200
    html = download.text
    assert html.lstrip().startswith("<!doctype html>")
    assert "Fixture HD" in html
    assert "Executive verdict" in html
    assert "Rebuffer Risk Score formula" in html
    assert "Glossary" in html
    assert "Checks passed" in html
    # Self-contained: the stylesheet is inline and no external stylesheet is referenced.
    assert "<style>" in html
    assert 'rel="stylesheet"' not in html


def test_a_report_carries_an_escalation_block_per_owner(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=4)
    origin.remove("low-seg2.ts")  # a 404 on a listed segment gives the CDN an owner block

    with TestClient(create_app()) as client:
        session_id = client.post(
            "/api/realtime/sessions", json={"playback_url": url, "channel_name": "Broken HD"}
        ).json()["id"]
        time.sleep(7.0)
        report_id = client.post(f"/api/realtime/sessions/{session_id}/report").json()["id"]
        html = client.get(f"/api/reports/{report_id}/download").text
        client.delete(f"/api/realtime/sessions/{session_id}")

    assert "Escalation — ready to send" in html
    assert "[TV Plus]" in html
    assert "Required fix" in html


def test_reports_are_listed_filtered_and_deleted(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    with TestClient(create_app()) as client:
        session_id = client.post(
            "/api/realtime/sessions", json={"playback_url": url, "channel_name": "Listing HD"}
        ).json()["id"]
        time.sleep(5.0)
        report_id = client.post(f"/api/realtime/sessions/{session_id}/report").json()["id"]
        client.delete(f"/api/realtime/sessions/{session_id}")

        listed = client.get("/api/reports").json()
        assert any(r["id"] == report_id for r in listed["reports"])

        filtered = client.get("/api/reports", params={"channel": "Listing"}).json()
        assert filtered["count"] >= 1

        empty = client.get("/api/reports", params={"channel": "no-such-channel"}).json()
        assert empty["count"] == 0

        assert client.delete(f"/api/reports/{report_id}").json() == {"deleted": True}
        assert client.delete(f"/api/reports/{report_id}").status_code == 404


def test_an_evidence_bundle_is_a_zip_with_playlists(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    with TestClient(create_app()) as client:
        session_id = client.post(
            "/api/realtime/sessions",
            json={"playback_url": url, "options": {"record_evidence": True}},
        ).json()["id"]
        time.sleep(5.0)
        bundle = client.get(f"/api/jobs/{session_id}/evidence.zip")
        client.delete(f"/api/realtime/sessions/{session_id}")

    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        names = archive.namelist()
    assert any(name.startswith("playlists/") for name in names)


# ---------------------------------------------------------------------------
# Aging
# ---------------------------------------------------------------------------


def test_an_aging_job_runs_reports_progress_and_is_cancellable(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=2, segment_count=4)

    with TestClient(create_app()) as client:
        presets = client.get("/api/aging/presets").json()
        assert presets["presets_minutes"] == [15, 30, 60, 180, 360, 720, 1440]

        created = client.post(
            "/api/aging/jobs",
            json={
                "playback_url": url,
                "channel_name": "Aging HD",
                "duration_minutes": 30,
                "options": {"record_evidence": True},
            },
        )
        assert created.status_code == 201
        job = created.json()
        assert job["type"] == "aging"
        assert job["ws_url"].startswith("/ws/jobs/")

        running = _wait_for(
            client, f"/api/aging/jobs/{job['id']}", lambda b: b["status"] == "RUNNING", timeout=20
        )
        assert running["remaining_s"] > 0

        listed = client.get("/api/aging/jobs").json()
        assert any(item["id"] == job["id"] for item in listed["jobs"])

        cancelled = client.delete(f"/api/aging/jobs/{job['id']}")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"

        # Cancelling still leaves a result to report on.
        report = client.post(f"/api/aging/jobs/{job['id']}/report")
        assert report.status_code == 200
        html = client.get(report.json()["url"].replace("/api", "/api")).text
        assert "Aging HD" in html


def test_an_aging_job_result_is_readable_from_the_database(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    with TestClient(create_app()) as client:
        job_id = client.post(
            "/api/aging/jobs",
            json={"playback_url": url, "channel_name": "Stored HD", "duration_minutes": 1},
        ).json()["id"]
        _wait_for(client, f"/api/aging/jobs/{job_id}", lambda b: b["status"] == "RUNNING", timeout=20)
        client.delete(f"/api/aging/jobs/{job_id}")

        result = client.get(f"/api/aging/jobs/{job_id}/result").json()
        assert "findings" in result
        assert "verdict" in result


def test_an_unknown_aging_job_is_a_404() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/aging/jobs/nope").status_code == 404


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------


def test_the_bulk_templates_download_in_every_format() -> None:
    with TestClient(create_app()) as client:
        for fmt, marker in (("csv", "channel_name"), ("json", "channels"), ("xlsx", "PK")):
            response = client.get(f"/api/bulk/template.{fmt}")
            assert response.status_code == 200
            assert marker.encode() in response.content
        assert client.get("/api/bulk/template.txt").status_code == 404


def test_bulk_validation_reports_row_level_errors() -> None:
    csv = (
        "channel_name,playback_url\n"
        "Good,https://cdn/a.m3u8\n"
        "Bad,not-a-url\n"
        ",https://cdn/c.m3u8\n"
    )
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/bulk/validate", files={"file": ("channels.csv", csv, "text/csv")}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["valid_count"] == 1
    assert len(body["errors"]) == 2


def test_a_bulk_job_analyses_every_channel_and_produces_a_consolidated_report(
    origin: FixtureServer,
) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)
    csv = f"channel_name,playback_url\nAlpha,{url}\nBeta,{url}\n"

    with TestClient(create_app()) as client:
        created = client.post(
            "/api/bulk/jobs",
            files={"file": ("channels.csv", csv, "text/csv")},
            data={"mode": "snapshot", "concurrency": "2"},
        )
        assert created.status_code == 201
        job_id = created.json()["id"]

        finished = _wait_for(
            client,
            f"/api/bulk/jobs/{job_id}",
            lambda b: b["status"] in ("COMPLETED", "FAILED", "CANCELLED"),
            timeout=300,
        )
        assert finished["status"] == "COMPLETED"
        assert finished["total"] == 2
        assert finished["completed"] == 2
        assert all(item["verdict_status"] for item in finished["items"])

        consolidated = client.get(f"/api/bulk/jobs/{job_id}/report.html")
        assert consolidated.status_code == 200
        assert "Bulk analysis" in consolidated.text
        assert "Owner summary" in consolidated.text
        assert "Alpha" in consolidated.text and "Beta" in consolidated.text

        bundle = client.get(f"/api/bulk/jobs/{job_id}/reports.zip")
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            names = archive.namelist()
        assert "consolidated.html" in names
        assert sum(1 for name in names if name.startswith("channels/")) == 2


def test_a_bulk_job_with_no_valid_row_is_rejected() -> None:
    csv = "channel_name,playback_url\nBad,not-a-url\n"
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/bulk/jobs",
            files={"file": ("channels.csv", csv, "text/csv")},
            data={"mode": "snapshot"},
        )
    assert response.status_code == 400
    assert "No row in the file is valid" in json.dumps(response.json())


def test_aging_mode_requires_a_duration() -> None:
    csv = "channel_name,playback_url\nA,https://cdn/a.m3u8\n"
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/bulk/jobs",
            files={"file": ("channels.csv", csv, "text/csv")},
            data={"mode": "aging"},
        )
    assert response.status_code == 400
    assert "duration_minutes" in response.json()["detail"]


def test_a_cancel_that_arrives_the_instant_a_job_starts_still_stops_it(
    origin: FixtureServer,
) -> None:
    """A stop is recorded on the handle, so it is honoured even before the session exists."""
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    with TestClient(create_app()) as client:
        job_id = client.post(
            "/api/aging/jobs",
            json={"playback_url": url, "channel_name": "Racy HD", "duration_minutes": 1440},
        ).json()["id"]

        # No wait: the cancel is sent while the session is still being built.
        cancelled = client.delete(f"/api/aging/jobs/{job_id}")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"
        assert cancelled.json()["elapsed_s"] < 60

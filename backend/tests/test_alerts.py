"""An aging run posts a webhook the moment it raises a finding worth waking someone for.

Once per issue per run, at or above the configured severity, never for what the channel
acknowledges, and never with the webhook's token-bearing URL returned to a page.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import alerts
from app.analysis.review import AckMatch
from app.main import create_app
from tests.fixtures.server import FixtureServer, build_simple_channel


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def _finding(
    rule: str = "HTTP-005", severity: str = "CRITICAL", variant: str = "v1"
) -> dict[str, Any]:
    return {
        "rule_id": rule,
        "title": "Segment listed in the playlist returns HTTP 404",
        "severity": severity,
        "variant": variant,
        "stream_layer": "PLAYBACK",
        "owner": "CDN",
        "owner_label": "CDN",
        "detail": "segment returned HTTP 404.",
        "fix": "Publish it.",
        "first_seen": "2026-10-13T03:00:00+00:00",
    }


def _settings(**overrides: Any) -> alerts.AlertSettings:
    return alerts.AlertSettings(
        enabled=True,
        webhook_url="https://hooks.example/services/T000/SECRET",
        min_severity="ERROR",
        job_types=["aging"],
        **overrides,
    )


@pytest.mark.asyncio()
async def test_one_message_per_issue_above_the_threshold_and_none_for_the_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, Any]] = []

    async def capture(url: str, payload: dict[str, Any]) -> tuple[bool, str]:
        sent.append(payload)
        return True, ""

    monkeypatch.setattr(alerts, "post", capture)
    job = alerts.JobAlerts(
        job_id="job-1",
        job_type="aging",
        channel="Channel 1",
        settings=_settings(public_origin="http://107.109.131.68:8080"),
        acknowledged=[AckMatch("MED-004", None)],
    )
    await job.observe(_finding(variant="v1"))
    await job.observe(_finding(variant="v2"))  # the same issue on another rendition
    await job.observe(_finding(rule="SEG-002", severity="WARN"))  # below the threshold
    await job.observe(_finding(rule="MED-004", severity="CRITICAL"))  # acknowledged

    assert len(sent) == 1
    payload = sent[0]
    assert payload["text"].startswith("[StreamLens] CRITICAL on Channel 1 (aging): HTTP-005")
    assert payload["link"] == "http://107.109.131.68:8080/#/channels/job-1"


@pytest.mark.asyncio()
async def test_a_job_type_not_chosen_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []

    async def capture(url: str, payload: dict[str, Any]) -> tuple[bool, str]:
        called.append(payload)
        return True, ""

    monkeypatch.setattr(alerts, "post", capture)
    job = alerts.JobAlerts(job_id="j", job_type="realtime", channel="c", settings=_settings())
    assert job.active is False
    await job.observe(_finding())
    assert called == []


def test_the_webhook_url_is_never_returned_to_a_page() -> None:
    described = _settings().describe()
    assert described["webhook_set"] is True
    assert described["webhook_host"] == "hooks.example"
    assert "SECRET" not in str(described)


def test_the_settings_endpoint_keeps_the_url_server_side(api: TestClient) -> None:
    response = api.put(
        "/api/settings/alerts",
        json={
            "enabled": True,
            "webhook_url": "https://hooks.example/services/T1/TOKEN",
            "min_severity": "CRITICAL",
            "job_types": ["aging"],
        },
    )
    assert response.status_code == 200
    assert "TOKEN" not in response.text
    kept = api.put("/api/settings/alerts", json={"enabled": False, "min_severity": "CRITICAL"})
    assert kept.json()["webhook_set"] is True, "an omitted URL keeps the stored one"
    cleared = api.put("/api/settings/alerts", json={"enabled": False, "webhook_url": ""})
    assert cleared.json()["webhook_set"] is False
    assert api.put("/api/settings/alerts", json={"webhook_url": "ftp://x"}).status_code == 422


@pytest.mark.asyncio()
async def test_a_real_aging_run_posts_its_critical_finding(
    origin: FixtureServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.schemas import JobOptionsIn
    from app.jobs.manager import job_manager

    sent: list[dict[str, Any]] = []

    async def capture(url: str, payload: dict[str, Any]) -> tuple[bool, str]:
        sent.append(payload)
        return True, ""

    async def configured() -> alerts.AlertSettings:
        return _settings()

    monkeypatch.setattr(alerts, "post", capture)
    monkeypatch.setattr(alerts, "load", configured)
    url = build_simple_channel(origin, variant_count=1, segment_count=6)
    origin.remove("low-seg2.ts")
    await job_manager.start()
    handle = await job_manager.submit(
        job_type="aging",
        playback_url=url,
        channel_name="Alerting channel",
        options=JobOptionsIn().to_session_options(duration_s=4.0, default_record=True),
    )
    assert handle.task is not None
    await handle.task
    assert any(p["rule_id"] == "HTTP-005" for p in sent)
    rules = [p["rule_id"] for p in sent]
    assert len(rules) == len(set(rules)), "one message per issue"

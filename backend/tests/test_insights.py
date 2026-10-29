"""Search findings across runs, and compare two runs side by side."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import session as db_session
from app.db.models import Finding as FindingRow
from app.db.models import Job as JobRow
from app.main import create_app

JOBS = ("insight-a", "insight-b", "insight-old")


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


def _finding(job: str, rule: str, severity: str, variant: str, count: int) -> FindingRow:
    now = dt.datetime.now(dt.UTC)
    return FindingRow(
        job_id=job,
        rule_id=rule,
        severity=severity,
        layer="http",
        stream_layer="PLAYBACK",
        owner="CDN",
        variant=variant,
        title=f"{rule} title",
        detail="d",
        root_cause="r",
        fix="f",
        rebuffer_impact="direct",
        count=count,
        first_seen=now,
        last_seen=now,
        evidence=[{"big": "x" * 500}],
    )


async def _seed() -> None:
    now = dt.datetime.now(dt.UTC)
    async with db_session.session_scope() as session:
        for job in JOBS:
            await session.execute(delete(FindingRow).where(FindingRow.job_id == job))
            await session.execute(delete(JobRow).where(JobRow.id == job))
        session.add_all(
            [
                JobRow(
                    id="insight-a",
                    type="aging",
                    status="COMPLETED",
                    channel_name="Insight News",
                    playback_url="https://edge-a.example/news/master.m3u8?t=1",
                    created_at=now - dt.timedelta(hours=2),
                    params={"ua_profile": "tizen9"},
                    verdict={"status": "STREAM_DEFECT", "risk_score": 70},
                ),
                JobRow(
                    id="insight-b",
                    type="aging",
                    status="COMPLETED",
                    channel_name="Insight News",
                    playback_url="https://edge-b.example/news/master.m3u8?t=2",
                    created_at=now - dt.timedelta(hours=1),
                    params={},
                ),
                JobRow(
                    id="insight-old",
                    type="realtime",
                    status="COMPLETED",
                    channel_name="Insight News",
                    playback_url="https://edge-a.example/news/master.m3u8",
                    created_at=now - dt.timedelta(days=40),
                    params={},
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                _finding("insight-a", "HTTP-005", "ERROR", "v1", 3),
                _finding("insight-a", "HTTP-005", "CRITICAL", "v2", 2),
                _finding("insight-a", "MED-004", "WARN", "v1", 1),
                _finding("insight-b", "HTTP-005", "ERROR", "v1", 1),
                _finding("insight-b", "SEG-002", "ERROR", "v1", 4),
                _finding("insight-old", "HTTP-005", "CRITICAL", "v1", 9),
            ]
        )


async def _clear() -> None:
    async with db_session.session_scope() as session:
        for job in JOBS:
            await session.execute(delete(FindingRow).where(FindingRow.job_id == job))
            await session.execute(delete(JobRow).where(JobRow.id == job))


@pytest.mark.asyncio()
async def test_search_finds_a_rule_across_runs_one_row_per_run_newest_first() -> None:
    from app.api.insights import search_findings

    await _seed()
    try:
        found = await search_findings(
            rule_id="http-005",
            channel="insight",
            host=None,
            severity=None,
            owner=None,
            since_days=7,
            limit=200,
        )
        mine = [r for r in found["results"] if r["job_id"] in JOBS]
        assert [r["job_id"] for r in mine] == ["insight-b", "insight-a"], (
            "newest first, the old run outside the window"
        )
        a = next(r for r in mine if r["job_id"] == "insight-a")
        assert (
            a["count"] == 5
            and a["severity"] == "CRITICAL"
            and sorted(a["renditions"]) == ["v1", "v2"]
        )

        on_host = await search_findings(
            rule_id=None,
            channel=None,
            host="edge-b.example",
            severity="ERROR",
            owner=None,
            since_days=7,
            limit=200,
        )
        assert {r["job_id"] for r in on_host["results"]} == {"insight-b"}

        severe = await search_findings(
            rule_id=None,
            channel="Insight News",
            host=None,
            severity="CRITICAL",
            owner="CDN",
            since_days=90,
            limit=200,
        )
        assert {r["job_id"] for r in severe["results"] if r["job_id"] in JOBS} == {
            "insight-a",
            "insight-old",
        }
    finally:
        await _clear()


@pytest.mark.asyncio()
async def test_compare_names_what_is_only_in_each_run_and_what_both_share() -> None:
    from app.api.insights import compare_runs

    await _seed()
    try:
        result = await compare_runs(a="insight-a", b="insight-b")
        assert [g["rule_id"] for g in result["only_a"]] == ["MED-004"]
        assert [g["rule_id"] for g in result["only_b"]] == ["SEG-002"]
        both = result["both"][0]
        assert both["rule_id"] == "HTTP-005"
        assert (both["count_a"], both["count_b"]) == (5, 1)
        assert (both["severity_a"], both["severity_b"]) == ("CRITICAL", "ERROR")
        assert result["a"]["verdict_status"] == "STREAM_DEFECT"
    finally:
        await _clear()

"""INFO-007: the analyzer states when it fell behind its own schedule.

A held event loop delays every poll and fetch, and the delay reads as the stream's. These tests
run a session against the fixture server twice: once untouched, where the analyzer keeps time
and says so, and once with a playlist handler that blocks the loop, where INFO-007 fires with
the measured delay.
"""

from __future__ import annotations

import time

import pytest

from app.analysis.engine import AnalysisSession, SessionOptions
from app.analysis.self_check import SelfCheck
from tests.fixtures.server import FixtureServer, build_simple_channel


def test_a_late_tick_and_an_overrun_handler_are_both_named() -> None:
    check = SelfCheck(lag_threshold_s=1.0)
    check.tick(1.0, 1.02)
    assert check.kept_time
    assert "kept time" in check.statement()

    check.tick(1.0, 2.5)
    check.handled("high", 4.0, 3.0)
    assert not check.kept_time
    text = check.statement()
    assert "1500 ms late" in text
    assert "high" in text and "3000 ms" in text
    summary = check.as_dict()
    assert summary["late_ticks"] == 1
    assert summary["handlers"]["high"]["over_budget"] == 1


async def test_a_session_that_kept_time_raises_no_info_007(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=2, segment_count=6)
    session = AnalysisSession(
        session_id="selfcheck-clean",
        playback_url=url,
        options=SessionOptions(duration_s=4.0, max_segment_samples=20),
    )
    result = await session.run()

    assert result.self_check["kept_time"] is True
    assert result.self_check["ticks"] > 0
    assert "INFO-007" not in {f.rule.id for f in result.findings}


async def test_a_blocked_event_loop_raises_info_007(
    origin: FixtureServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=6)
    session = AnalysisSession(
        session_id="selfcheck-late",
        playback_url=url,
        options=SessionOptions(duration_s=5.0, max_segment_samples=20),
    )
    original = session._handle_snapshot
    blocked: list[bool] = []

    async def blocking(context, snapshot):  # type: ignore[no-untyped-def]
        if not blocked:
            blocked.append(True)
            # A detector that holds the loop: nothing else runs while it sleeps.
            time.sleep(3.5)
        await original(context, snapshot)

    monkeypatch.setattr(session, "_handle_snapshot", blocking)
    result = await session.run()

    late = [f for f in result.findings if f.rule.id == "INFO-007"]
    assert len(late) == 1
    assert result.self_check["kept_time"] is False
    assert "fell behind its own schedule" in late[0].detail
    # Both measurements see it: the supervisor woke late and the handler outran its interval.
    assert result.self_check["late_ticks"] >= 1 or any(
        h["over_budget"] for h in result.self_check["handlers"].values()
    )

"""Virtual Player Buffer, correlation, attribution and verdict."""

from __future__ import annotations

import datetime as dt

from app.analysis import attribution, correlate, vpb
from app.analysis.rules import catalogue as R
from app.analysis.rules.base import Owner, Severity, StreamLayer
from app.analysis.verdict import VerdictStatus, risk_score
from app.analysis.verdict import build as build_verdict
from app.config import Thresholds, VpbMode

T = Thresholds()
START = dt.datetime(2026, 9, 11, 12, 0, 0, tzinfo=dt.UTC)


def at(seconds: float) -> dt.datetime:
    return START + dt.timedelta(seconds=seconds)


def delivery(msn: int, completed_s: float, duration: float = 6.0, available: bool = True):  # type: ignore[no-untyped-def]
    return vpb.SegmentDelivery(
        msn=msn, completed_at=at(completed_s), duration_s=duration, available=available
    )


def test_segments_arriving_at_real_time_never_empty_the_buffer() -> None:
    # Three segments fill the startup buffer, then one arrives per six seconds of playback.
    deliveries = [delivery(0, 0.5), delivery(1, 1.0), delivery(2, 1.5)]
    deliveries += [delivery(n, 1.5 + (n - 2) * 6.0) for n in range(3, 12)]

    result = vpb.run("720p", deliveries, target_duration=6.0, thresholds=T, end_at=at(60))
    assert result.started_playback is True
    assert result.stall_s == 0.0
    assert result.rebuffer_ratio == 0.0
    assert result.counted_stalls == []


def test_segments_arriving_slower_than_real_time_drain_the_buffer_to_zero() -> None:
    deliveries = [delivery(0, 0.5), delivery(1, 1.0), delivery(2, 1.5)]
    # From here every segment takes ten wall seconds to deliver six seconds of media.
    deliveries += [delivery(n, 1.5 + (n - 2) * 10.0) for n in range(3, 10)]

    result = vpb.run("720p", deliveries, target_duration=6.0, thresholds=T, end_at=at(90))
    assert result.started_playback is True
    assert result.stall_s > 0
    assert result.rebuffer_ratio > 0
    assert len(result.stalls) >= 1


def test_the_rebuffer_ratio_crosses_the_threshold_and_produces_a_finding() -> None:
    deliveries = [delivery(0, 0.5), delivery(1, 1.0), delivery(2, 1.5)]
    deliveries += [delivery(n, 1.5 + (n - 2) * 20.0) for n in range(3, 8)]

    result = vpb.run("720p", deliveries, target_duration=6.0, thresholds=T, end_at=at(120))
    assert result.rebuffer_ratio > T.rebuffer_ratio_threshold

    findings = vpb.findings_for(result, layer=StreamLayer.PLAYBACK, thresholds=T)
    ratio_finding = next(f for f in findings if f.rule.id == "VPB-001")
    assert ratio_finding.severity is Severity.CRITICAL
    assert "rebuffering ratio" in ratio_finding.detail


def test_unavailable_segments_add_nothing_and_open_an_outage_window() -> None:
    deliveries = [delivery(0, 0.5), delivery(1, 1.0), delivery(2, 1.5)]
    deliveries += [delivery(n, 2.0 + n, available=False) for n in range(3, 10)]

    result = vpb.run("720p", deliveries, target_duration=6.0, thresholds=T, end_at=at(60))
    assert result.segments_unavailable == 7
    assert result.outage_windows
    findings = vpb.findings_for(result, layer=StreamLayer.PLAYBACK, thresholds=T)
    assert "VPB-004" in {f.rule.id for f in findings}


def test_a_buffer_above_the_maximum_emits_buffer_too_long() -> None:
    deliveries = [delivery(n, 0.1 * n, duration=10.0) for n in range(20)]
    result = vpb.run("720p", deliveries, target_duration=6.0, thresholds=T, end_at=at(5))
    assert result.buffer_too_long_events
    findings = vpb.findings_for(result, layer=StreamLayer.PLAYBACK, thresholds=T)
    assert "VPB-003" in {f.rule.id for f in findings}


def test_outage_only_mode_counts_only_stalls_inside_an_outage() -> None:
    deliveries = [delivery(0, 0.5), delivery(1, 1.0), delivery(2, 1.5)]
    deliveries += [delivery(n, 1.5 + (n - 2) * 12.0) for n in range(3, 8)]

    strict = vpb.run(
        "720p", deliveries, target_duration=6.0, thresholds=T, mode=VpbMode.STRICT, end_at=at(90)
    )
    outage_only = vpb.run(
        "720p",
        deliveries,
        target_duration=6.0,
        thresholds=T,
        mode=VpbMode.OUTAGE_ONLY,
        end_at=at(90),
    )
    assert len(strict.counted_stalls) >= 1
    assert outage_only.counted_stalls == []


def test_playback_does_not_start_before_the_startup_buffer_is_full() -> None:
    result = vpb.run("720p", [delivery(0, 0.5)], target_duration=6.0, thresholds=T, end_at=at(30))
    assert result.started_playback is False
    assert result.playing_s == 0.0


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def _finding(rule, *, variant: str, first: float, last: float):  # type: ignore[no-untyped-def]
    finding = rule.raise_finding("measured", variant=variant, at=at(first))
    finding.last_seen = at(last)
    return finding


def test_a_stall_is_matched_to_the_stale_playlist_that_preceded_it() -> None:
    stale = _finding(R.MED_STALE, variant="720p", first=100, last=120)
    unrelated = _finding(R.MST_RUNG_STEP, variant="360p", first=5, last=5)
    stall = correlate.StallEvent(
        started_at=at(114), ended_at=at(118), variant="720p", source="player", duration_s=4.1
    )

    chains = correlate.correlate([stall], [stale, unrelated], target_duration=6.0, thresholds=T)
    assert len(chains) == 1
    assert chains[0].primary is not None
    assert chains[0].primary.rule.id == "MED-004"
    described = chains[0].describe()
    assert "MED-004" in described
    assert "player stall" in described


def test_a_stall_with_nothing_measured_in_its_window_says_so_definitely() -> None:
    stall = correlate.StallEvent(
        started_at=at(200), ended_at=at(203), variant="720p", source="vpb", duration_s=3.0
    )
    chains = correlate.correlate([stall], [], target_duration=6.0, thresholds=T)
    assert chains[0].causes == []
    assert "No stream-side event was measured" in chains[0].describe()


def test_findings_on_another_rung_are_not_offered_as_a_cause() -> None:
    other = _finding(R.MED_STALE, variant="360p", first=100, last=120)
    stall = correlate.StallEvent(
        started_at=at(114), ended_at=at(118), variant="720p", source="vpb", duration_s=4.0
    )
    chains = correlate.correlate([stall], [other], target_duration=6.0, thresholds=T)
    assert chains[0].causes == []


def test_an_incident_opens_only_after_the_hysteresis_window() -> None:
    tracker = correlate.IncidentTracker(T)
    assert tracker.observe(kind="stale", variant="720p", at=at(0), degraded=True) is None
    assert tracker.observe(kind="stale", variant="720p", at=at(5), degraded=True) is None
    opened = tracker.observe(kind="stale", variant="720p", at=at(11), degraded=True)
    assert opened is not None
    assert opened.started_at == at(0)


def test_an_incident_closes_only_after_the_clear_window() -> None:
    tracker = correlate.IncidentTracker(T)
    tracker.observe(kind="stale", variant="720p", at=at(0), degraded=True)
    tracker.observe(kind="stale", variant="720p", at=at(11), degraded=True)
    assert tracker.observe(kind="stale", variant="720p", at=at(15), degraded=False) is None
    closed = tracker.observe(kind="stale", variant="720p", at=at(80), degraded=False)
    assert closed is not None
    assert closed.ended_at == at(15)
    assert closed.duration_s == 15.0


def test_a_brief_blip_never_opens_an_incident() -> None:
    tracker = correlate.IncidentTracker(T)
    tracker.observe(kind="slow", variant="720p", at=at(0), degraded=True)
    tracker.observe(kind="slow", variant="720p", at=at(3), degraded=False)
    assert tracker.incidents == []


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_a_defect_present_at_origin_is_owned_by_the_packager() -> None:
    origin = R.MED_STALE.raise_finding("m", variant="720p", stream_layer=StreamLayer.ORIGIN)
    cdn = R.MED_STALE.raise_finding("m", variant="720p", stream_layer=StreamLayer.CDN)
    findings, diffs = attribution.attribute(
        [origin, cdn], layers_analysed={StreamLayer.ORIGIN, StreamLayer.CDN}
    )
    assert all(f.owner is Owner.PACKAGER for f in findings)
    assert diffs[0].first_layer == "ORIGIN"
    assert diffs[0].statement.startswith("Present at ORIGIN")


def test_a_defect_that_first_appears_at_the_cdn_is_owned_by_the_cdn() -> None:
    cdn = R.MED_STALE.raise_finding("m", variant="720p", stream_layer=StreamLayer.CDN)
    findings, diffs = attribution.attribute(
        [cdn], layers_analysed={StreamLayer.ORIGIN, StreamLayer.CDN}
    )
    assert findings[0].owner is Owner.CDN
    assert "First appears at CDN" in diffs[0].statement


def test_a_defect_introduced_by_ssai_is_owned_by_the_ssai_vendor() -> None:
    ssai = R.MED_STALE.raise_finding("m", variant="720p", stream_layer=StreamLayer.SSAI)
    findings, diffs = attribution.attribute(
        [ssai], layers_analysed={StreamLayer.ORIGIN, StreamLayer.CDN, StreamLayer.SSAI}
    )
    assert findings[0].owner is Owner.SSAI
    assert "Introduced by SSAI" in diffs[0].statement


def test_without_comparison_urls_the_headers_prove_the_owner() -> None:
    finding = R.SEQ_MSN_BACKWARDS.raise_finding(
        "m",
        variant="720p",
        evidence={"headers": {"Age": "18", "X-Cache": "HIT from edge-1"}},
    )
    findings, diffs = attribution.attribute([finding], layers_analysed={StreamLayer.PLAYBACK})
    assert findings[0].owner is Owner.CDN
    assert diffs == []
    proof = next(e for e in findings[0].evidence if "attribution_proof" in e)
    assert "served this from cache" in proof["attribution_proof"]


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def test_a_clean_window_produces_the_exact_no_defect_sentence() -> None:
    verdict = build_verdict(
        findings=[],
        chains=[],
        incidents=[],
        thresholds=T,
        window_seconds=1800,
        playlists_checked=612,
        segments_checked=1804,
        measured_ratio=0.0,
    )
    assert verdict.status is VerdictStatus.NO_STREAM_SIDE_DEFECT
    assert verdict.headline == (
        "No stream-side defect detected in 30 minutes across 612 playlists and 1804 segments. "
        "Rebuffering on this channel is not caused by the stream as delivered to the analyzer. "
        "Escalate to Samsung player/device investigation with this report attached."
    )
    assert verdict.owner == "SAMSUNG_PLAYER"


def test_the_primary_root_cause_is_the_highest_ranked_correlated_defect() -> None:
    stale = _finding(R.MED_STALE, variant="720p", first=100, last=160)
    stale.count = 12
    cosmetic = _finding(R.MST_DUPLICATE_RUNG, variant="720p", first=1, last=1)
    stall = correlate.StallEvent(
        started_at=at(120), ended_at=at(126), variant="720p", source="vpb", duration_s=6.0
    )
    chains = correlate.correlate([stall], [stale, cosmetic], target_duration=6.0, thresholds=T)

    verdict = build_verdict(
        findings=[stale, cosmetic],
        chains=chains,
        incidents=[],
        thresholds=T,
        window_seconds=300,
        playlists_checked=50,
        segments_checked=120,
        measured_ratio=0.31,
    )
    assert verdict.status is VerdictStatus.REBUFFERING_STREAM_DEFECT
    assert verdict.primary is not None
    assert verdict.primary.finding.rule.id == "MED-004"
    assert verdict.required_fix == R.MED_STALE.fix
    assert verdict.risk_score > 50


def test_a_defect_with_no_stall_is_reported_as_a_risk_rather_than_a_rebuffer() -> None:
    warn = _finding(R.MST_RUNG_STEP, variant="720p", first=1, last=1)
    verdict = build_verdict(
        findings=[warn],
        chains=[],
        incidents=[],
        thresholds=T,
        window_seconds=300,
        playlists_checked=50,
        segments_checked=120,
        measured_ratio=0.0,
    )
    assert verdict.status is VerdictStatus.REBUFFER_RISK_STREAM_DEFECT


def test_the_risk_score_is_bounded_and_follows_the_documented_formula() -> None:
    assert (
        risk_score(
            worst_severity=Severity.PASS,
            measured_ratio=0.0,
            incident_seconds=0,
            window_seconds=600,
            direct_occurrences=0,
            thresholds=T,
        )
        == 0
    )
    assert (
        risk_score(
            worst_severity=Severity.CRITICAL,
            measured_ratio=1.0,
            incident_seconds=600,
            window_seconds=600,
            direct_occurrences=1000,
            thresholds=T,
        )
        == 100
    )

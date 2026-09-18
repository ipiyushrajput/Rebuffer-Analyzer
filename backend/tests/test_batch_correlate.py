"""Lining aging findings up against the minutes viewers were rebuffering.

Two systems on two machines are being compared, so the failure mode this file exists to
prevent is a silent one: a timezone read wrongly would place an event an hour away from a
spike confidently inside it, and the report would state a correlation that never happened.
Every assertion here is about a time boundary or about saying plainly that nothing matched.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.batch import correlate
from app.cascada.series import Point

START = dt.datetime(2026, 9, 14, 3, 0, tzinfo=dt.UTC)
THRESHOLD = 0.25


def points(values: list[float | None], start: dt.datetime = START) -> list[Point]:
    """One reading per minute from `start`."""
    return [
        Point(at=start + dt.timedelta(minutes=index), value=value)
        for index, value in enumerate(values)
    ]


def event(minutes: float, label: str = "SEG-001 Segment returned HTTP 404") -> correlate.Event:
    return correlate.Event(at=START + dt.timedelta(minutes=minutes), kind="finding", label=label)


# -- spike windows -----------------------------------------------------------


def test_consecutive_bad_minutes_are_one_window_not_several() -> None:
    """A ten-minute stretch of bad delivery is one thing to investigate, not ten."""
    spikes = correlate.spike_windows(points([0.1, 0.9, 1.4, 0.8, 0.1]), THRESHOLD)

    assert len(spikes) == 1
    assert spikes[0].start == START + dt.timedelta(minutes=1)
    assert spikes[0].end == START + dt.timedelta(minutes=3)
    assert spikes[0].minutes == 3
    assert spikes[0].peak_pct == pytest.approx(1.4)


def test_a_minute_at_the_threshold_is_not_a_spike() -> None:
    """Above, not at — the same comparison the scan uses to select a channel."""
    assert correlate.spike_windows(points([0.25, 0.25]), THRESHOLD) == []
    assert len(correlate.spike_windows(points([0.26]), THRESHOLD)) == 1


def test_a_minute_cascada_never_measured_breaks_the_run_rather_than_extending_it() -> None:
    """A gap is not evidence that the channel was still rebuffering."""
    spikes = correlate.spike_windows(points([0.9, None, 0.9]), THRESHOLD)

    assert len(spikes) == 2
    assert all(spike.minutes == 1 for spike in spikes)


def test_a_window_shorter_than_the_minimum_is_not_reported() -> None:
    assert correlate.spike_windows(points([0.9, 0.1, 0.9, 0.9]), THRESHOLD, min_minutes=2) == [
        correlate.Spike(
            start=START + dt.timedelta(minutes=2),
            end=START + dt.timedelta(minutes=3),
            peak_pct=0.9,
            minutes=2,
        )
    ]


def test_readings_out_of_order_still_produce_the_right_window() -> None:
    """The series is sorted before it is walked; a response is not trusted to be ordered."""
    shuffled = list(reversed(points([0.9, 0.9, 0.1])))
    spikes = correlate.spike_windows(shuffled, THRESHOLD)

    assert len(spikes) == 1
    assert spikes[0].start == START


# -- UTC ---------------------------------------------------------------------


@pytest.mark.parametrize("offset_hours", [0, 9, -7, 5.5])
def test_the_same_instant_in_any_zone_lands_in_the_same_window(offset_hours: float) -> None:
    """
    The aging host and CASCADA are different machines and may report different zones.

    An event at the same instant must match whatever zone it arrives in. Reading a `+09:00`
    timestamp as if it were UTC would place it nine hours away and match it to nothing — or,
    worse, to the wrong window.
    """
    spike = correlate.spike_windows(points([0.9, 0.9, 0.9]), THRESHOLD)[0]
    instant = START + dt.timedelta(minutes=1)
    zone = dt.timezone(dt.timedelta(hours=offset_hours))

    assert spike.covers(instant.astimezone(zone), tolerance_s=0) is True


def test_a_naive_timestamp_is_read_as_utc_rather_than_as_local_time() -> None:
    """The analyzer stores UTC; SQLite hands it back without a zone."""
    spike = correlate.spike_windows(points([0.9]), THRESHOLD)[0]
    naive = START.replace(tzinfo=None)

    assert spike.covers(naive, tolerance_s=0) is True
    assert correlate.as_utc(naive) == START


def test_an_event_an_hour_away_never_matches_however_it_is_spelled() -> None:
    """The case a timezone bug would produce: a confident correlation that is not real."""
    spike = correlate.spike_windows(points([0.9]), THRESHOLD)[0]
    an_hour_later = START + dt.timedelta(hours=1)

    assert spike.covers(an_hour_later, tolerance_s=120) is False
    assert spike.covers(an_hour_later.astimezone(dt.timezone(dt.timedelta(hours=5))), 120) is False


# -- the tolerance -----------------------------------------------------------


def test_an_event_just_outside_the_window_matches_within_the_tolerance() -> None:
    """Two clocks on two machines do not agree to the second."""
    spike = correlate.spike_windows(points([0.9]), THRESHOLD)[0]

    assert spike.covers(START - dt.timedelta(seconds=90), tolerance_s=120) is True
    assert spike.covers(START - dt.timedelta(seconds=90), tolerance_s=30) is False


def test_the_window_covers_the_whole_of_its_last_minute() -> None:
    """A reading timestamped 03:05 describes 03:05:00 to 03:05:59."""
    spike = correlate.spike_windows(points([0.9, 0.9]), THRESHOLD)[0]
    within_last_minute = START + dt.timedelta(minutes=1, seconds=45)

    assert spike.covers(within_last_minute, tolerance_s=0) is True


def test_a_zero_tolerance_still_matches_an_event_inside_the_window() -> None:
    spike = correlate.spike_windows(points([0.9, 0.9, 0.9]), THRESHOLD)[0]
    assert spike.covers(START + dt.timedelta(minutes=1), tolerance_s=0) is True


# -- what is reported --------------------------------------------------------


def test_a_window_with_nothing_in_it_says_so_rather_than_being_left_blank() -> None:
    """
    An unexplained spike is a finding of its own.

    Saying nothing would read as "no spike"; inventing a cause would be worse than both.
    """
    matches = correlate.match_events(
        correlate.spike_windows(points([0.9, 0.9]), THRESHOLD), [], tolerance_s=120
    )

    assert len(matches) == 1
    assert matches[0].matched is False
    assert "no aging event was captured in window" in matches[0].sentence()


def test_a_matched_window_names_what_was_captured_and_how_often() -> None:
    spikes = correlate.spike_windows(points([0.9, 0.9, 0.9]), THRESHOLD)
    events = [event(0.0), event(1.0), event(2.0, "MED-004 Playlist is stale")]

    matched = correlate.match_events(spikes, events, tolerance_s=60)[0]
    sentence = matched.sentence()

    assert matched.matched is True
    assert "3 aging event(s) in window" in sentence
    assert "SEG-001 Segment returned HTTP 404 ×2" in sentence
    assert "MED-004 Playlist is stale" in sentence


def test_the_sentence_states_the_window_and_the_peak_without_a_cause() -> None:
    """Evidence only: what happened and when, never why."""
    spikes = correlate.spike_windows(points([2.5, 4.0]), THRESHOLD)
    sentence = correlate.match_events(spikes, [event(0.0)], tolerance_s=60)[0].sentence()

    assert "2026-09-14 03:00–03:01 UTC" in sentence
    assert "peak 4.000 %" in sentence
    assert "because" not in sentence.lower()


def test_an_event_between_two_close_windows_is_reported_in_both() -> None:
    """It is evidence for both, and dropping it from one would hide it."""
    spikes = correlate.spike_windows(points([0.9, 0.1, 0.1, 0.9]), THRESHOLD)
    assert len(spikes) == 2

    matches = correlate.match_events(spikes, [event(1.6)], tolerance_s=300)
    assert [match.matched for match in matches] == [True, True]


# -- the whole correlation ---------------------------------------------------


def test_findings_and_incidents_both_become_events_with_a_time_on_them() -> None:
    findings = [
        {
            "rule_id": "SEG-001",
            "title": "Segment returned HTTP 404",
            "severity": "ERROR",
            "first_seen": (START + dt.timedelta(minutes=1)).isoformat(),
            "detail": "the edge does not hold it",
        }
    ]
    incidents = [
        {
            "kind": "rebuffer",
            "started_at": (START + dt.timedelta(minutes=1)).isoformat(),
            "duration_s": 4,
        }
    ]

    events = correlate.events_from(findings, incidents)

    assert [e.kind for e in events] == ["finding", "incident"]
    assert all(e.at.tzinfo is not None for e in events)


def test_an_event_with_no_usable_timestamp_is_left_out_rather_than_guessed() -> None:
    """It cannot be placed on a timeline, and placing it anyway would invent evidence."""
    events = correlate.events_from(
        [
            {"rule_id": "X", "title": "no time"},
            {"rule_id": "Y", "title": "bad", "first_seen": "nope"},
        ],
        [{"kind": "incident"}],
    )
    assert events == []


def test_the_correlation_counts_what_matched_and_what_did_not() -> None:
    series = points([0.9, 0.9, 0.05, 0.05, 1.2])
    findings = [
        {
            "rule_id": "SEG-001",
            "title": "Segment returned HTTP 404",
            "first_seen": (START + dt.timedelta(minutes=1)).isoformat(),
        }
    ]

    result = correlate.correlation_for(
        points=series,
        findings=findings,
        incidents=[],
        threshold_pct=THRESHOLD,
        tolerance_s=60,
    )

    assert result["spike_count"] == 2
    assert result["matched_count"] == 1
    assert result["aging_event_count"] == 1
    assert result["windows"][0]["matched"] is True
    assert result["windows"][1]["matched"] is False
    assert "no aging event was captured" in result["windows"][1]["sentence"]


def test_a_channel_that_never_went_above_threshold_has_no_windows() -> None:
    result = correlate.correlation_for(
        points=points([0.01, 0.02, 0.01]),
        findings=[{"rule_id": "X", "title": "y", "first_seen": START.isoformat()}],
        incidents=[],
        threshold_pct=THRESHOLD,
        tolerance_s=60,
    )

    assert result["spike_count"] == 0
    assert result["windows"] == []

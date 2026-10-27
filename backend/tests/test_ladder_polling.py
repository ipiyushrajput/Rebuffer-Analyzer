"""Renditions are polled together, and the ladder is compared with itself one round at a time.

Each rendition used to run its own poll loop, and the cross-variant check compared whatever
each one had fetched last — snapshots seconds apart. A packager publishing one rung a moment
before the next then read as a ladder whose discontinuity counters disagreed (SEQ-009), whose
media sequence numbers had spread (SEQ-008), and so on. These tests hold the analyzer to one
round per comparison, and to a disagreement that persists across rounds before it is reported.
"""

from __future__ import annotations

import datetime as dt

from app.analysis.collectors.playlist_poller import LadderPoller, PlaylistPoller, PollTarget
from app.analysis.engine import AnalysisSession, SessionOptions
from app.analysis.rules.base import StreamLayer
from app.analysis.rules.sequence import (
    VariantSnapshot,
    check_cross_variant,
    check_sequence_transition,
)
from app.config import Thresholds
from app.hls.playlist import parse_media
from app.net.fetcher import Fetcher
from tests.fixtures.server import FixtureServer, Route, build_simple_channel
from tests.fixtures.synth import PlaylistSpec, render_media_playlist

SEGMENT_S = 2.0


def _media(spec: PlaylistSpec, url: str = "https://cdn.example/v.m3u8"):  # type: ignore[no-untyped-def]
    return parse_media(render_media_playlist(spec), url)


async def _run(url: str, duration: float = 6.0) -> AnalysisSession:
    session = AnalysisSession(
        session_id="ladder",
        playback_url=url,
        options=SessionOptions(duration_s=duration, max_segment_samples=40),
    )
    await session.run()
    return session


def _rule_ids(session: AnalysisSession) -> set[str]:
    return {finding.rule.id for finding in session.collector.all()}


# -- the round ---------------------------------------------------------------------------


async def test_a_round_requests_every_rendition_together(origin: FixtureServer) -> None:
    build_simple_channel(origin, variant_count=3, segment_count=6, segment_duration=SEGMENT_S)
    fetcher = Fetcher()
    rounds: list[list[tuple[int, dt.datetime, dt.datetime]]] = []

    async def on_round(round_no, polled):  # type: ignore[no-untyped-def]
        rounds.append([(s.round, s.requested_at, s.at) for _, s in polled])

    pollers = [
        PlaylistPoller(
            PollTarget(variant=n, url=origin.url(f"{n}.m3u8")), fetcher, on_snapshot=None
        )  # type: ignore[arg-type]
        for n in ("low", "mid", "high")
    ]
    ladder = LadderPoller(pollers, on_round=on_round)
    try:
        for _ in range(2):
            await on_round(0, await ladder.poll_round())
    finally:
        await fetcher.aclose()

    assert [{r for r, _, _ in polled} for polled in rounds] == [{1}, {2}]
    for polled in rounds:
        requested = [req for _, req, _ in polled]
        # Every request of a round goes out before any response of it is waited on.
        assert max(requested) <= min(at for _, _, at in polled)
    assert ladder.interval_s == SEGMENT_S / 2 or ladder.interval_s == 1.0


# -- SEQ-009 across rounds ---------------------------------------------------------------


async def test_a_ladder_that_disagrees_round_after_round_is_reported(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=3, segment_count=6, segment_duration=SEGMENT_S)
    # The mid rung carries a discontinuity counter one ahead of the others, on every poll.
    origin.add_text(
        "mid.m3u8",
        render_media_playlist(
            PlaylistSpec(
                segment_count=6,
                segment_duration=SEGMENT_S,
                target_duration=int(SEGMENT_S),
                segment_prefix="mid-seg",
                discontinuity_sequence=1,
            )
        ),
    )
    session = await _run(url)

    assert "SEQ-009" in _rule_ids(session)
    finding = next(f for f in session.collector.all() if f.rule.id == "SEQ-009")
    sample = finding.evidence[0]
    assert sample["confirmed_rounds"] >= 2
    assert sample["round_spread_ms"] < 1500


async def test_a_rung_caught_mid_publication_for_one_round_is_not_reported(
    origin: FixtureServer,
) -> None:
    url = build_simple_channel(origin, variant_count=3, segment_count=6, segment_duration=SEGMENT_S)
    calls = {"n": 0}
    ahead = render_media_playlist(
        PlaylistSpec(
            segment_count=6,
            segment_duration=SEGMENT_S,
            target_duration=int(SEGMENT_S),
            segment_prefix="mid-seg",
            discontinuity_sequence=1,
        )
    ).encode()
    in_step = render_media_playlist(
        PlaylistSpec(
            segment_count=6,
            segment_duration=SEGMENT_S,
            target_duration=int(SEGMENT_S),
            segment_prefix="mid-seg",
        )
    ).encode()

    def mid(_query: dict[str, list[str]]) -> bytes:
        calls["n"] += 1
        # Only the first poll of the run sees the rung ahead of the ladder.
        return ahead if calls["n"] == 1 else in_step

    origin.add("mid.m3u8", Route(body=mid))
    session = await _run(url)

    assert calls["n"] >= 3, "the rung was polled in several rounds"
    assert "SEQ-009" not in _rule_ids(session)
    assert "SEQ-008" not in _rule_ids(session)


async def test_a_clean_ladder_raises_no_cross_variant_finding(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=3, segment_count=6, segment_duration=SEGMENT_S)
    session = await _run(url)
    assert not _rule_ids(session) & {"SEQ-008", "SEQ-009", "SEQ-010", "SEQ-011", "SEQ-012"}


# -- SEQ-007: discontinuities that left the window --------------------------------------


def test_dsn_advancing_by_the_tags_that_rolled_off_is_not_a_jump() -> None:
    previous = _media(PlaylistSpec(media_sequence=10, discontinuity_at={0, 1}, segment_count=6))
    current = _media(PlaylistSpec(media_sequence=12, discontinuity_sequence=2, segment_count=6))
    findings = check_sequence_transition(
        previous, current, variant="v", layer=StreamLayer.PLAYBACK, elapsed_s=12.0
    )
    assert "SEQ-007" not in {f.rule.id for f in findings}


def test_dsn_advancing_past_the_tags_that_rolled_off_is_a_jump() -> None:
    previous = _media(PlaylistSpec(media_sequence=10, discontinuity_at={0}, segment_count=6))
    current = _media(PlaylistSpec(media_sequence=11, discontinuity_sequence=3, segment_count=6))
    findings = check_sequence_transition(
        previous, current, variant="v", layer=StreamLayer.PLAYBACK, elapsed_s=6.0
    )
    jump = [f for f in findings if f.rule.id == "SEQ-007"]
    assert jump and jump[0].evidence[0]["discontinuities_rolled_off"] == 1


# -- SEQ-003: a late poll is not a jump -------------------------------------------------


def test_msn_advance_that_the_time_between_polls_explains_is_not_a_jump() -> None:
    previous = _media(PlaylistSpec(media_sequence=100, segment_count=5))
    current = _media(PlaylistSpec(media_sequence=110, segment_count=5))
    late = check_sequence_transition(
        previous, current, variant="v", layer=StreamLayer.PLAYBACK, elapsed_s=60.0
    )
    quick = check_sequence_transition(
        previous, current, variant="v", layer=StreamLayer.PLAYBACK, elapsed_s=3.0
    )
    assert "SEQ-003" not in {f.rule.id for f in late}
    assert "SEQ-003" in {f.rule.id for f in quick}


# -- SEQ-001: re-signed segment URIs ----------------------------------------------------


def test_segments_resigned_on_every_poll_still_roll_off_by_path() -> None:
    def signed(msn: int, token: str) -> str:
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6", f"#EXT-X-MEDIA-SEQUENCE:{msn}"]
        for i in range(4):
            lines += ["#EXTINF:6.0,", f"seg{msn + i}.ts?token={token}"]
        return "\n".join(lines) + "\n"

    previous = parse_media(signed(10, "a"), "https://cdn.example/v.m3u8")
    current = parse_media(signed(11, "b"), "https://cdn.example/v.m3u8")
    findings = check_sequence_transition(
        previous, current, variant="v", layer=StreamLayer.PLAYBACK, elapsed_s=6.0
    )
    assert "SEQ-001" not in {f.rule.id for f in findings}


# -- SEQ-010 and SEQ-012 -----------------------------------------------------------------


def _snap(variant: str, playlist) -> VariantSnapshot:  # type: ignore[no-untyped-def]
    return VariantSnapshot(variant=variant, playlist=playlist, at=dt.datetime.now(dt.UTC))


def test_durations_within_the_tolerance_are_the_same_segment() -> None:
    a = _media(PlaylistSpec(durations=[6.0] * 6))
    b = _media(PlaylistSpec(durations=[6.006] * 6))
    c = _media(PlaylistSpec(durations=[6.0, 6.0, 4.0, 6.0, 6.0, 6.0]))
    t = Thresholds()
    within = check_cross_variant(
        [_snap("a", a), _snap("b", b)], layer=StreamLayer.PLAYBACK, thresholds=t
    )
    beyond = check_cross_variant(
        [_snap("a", a), _snap("c", c)], layer=StreamLayer.PLAYBACK, thresholds=t
    )
    assert "SEQ-010" not in {f.rule.id for f in within}
    assert "SEQ-010" in {f.rule.id for f in beyond}


def test_program_date_times_are_compared_at_one_media_sequence_number() -> None:
    # Rung b's window has slid one segment further; its first PDT is 6 s later, and the
    # timeline is the same.
    a = _media(PlaylistSpec(media_sequence=10, program_date_time="2026-09-11T10:00:00.000Z"))
    b = _media(PlaylistSpec(media_sequence=11, program_date_time="2026-09-11T10:00:06.000Z"))
    off = _media(PlaylistSpec(media_sequence=11, program_date_time="2026-09-11T10:00:09.000Z"))
    t = Thresholds()
    same = check_cross_variant(
        [_snap("a", a), _snap("b", b)], layer=StreamLayer.PLAYBACK, thresholds=t
    )
    shifted = check_cross_variant(
        [_snap("a", a), _snap("off", off)], layer=StreamLayer.PLAYBACK, thresholds=t
    )
    assert "SEQ-012" not in {f.rule.id for f in same}
    assert "SEQ-012" in {f.rule.id for f in shifted}

"""Demuxed ladders: what a video segment is supposed to contain, and where the skew is.

Three defects close here, all of them on channels whose audio is a rendition of its own —
which is every Samsung TV Plus CMAF channel, protected or clear.

1. `AUD-003` ("Muxed segment carries video and no audio elementary stream") fired CRITICAL
   on every video segment of every demuxed channel. A demuxed video segment carrying no
   audio is the format working, not a defect. The only gate was `is_audio_only`, and nothing
   in the segment path knew the ladder's layout — which `rules/master.py` computes for
   `MST-012` and then discards.
2. `check_demuxed_audio_coverage` was written, declared as `AUD-006` and `AUD-007`, and
   never called by anything. Both rules were unreachable.
3. `AV-001` to `AV-005` never ran on a demuxed ladder at all. They read
   `SegmentAnalysis.av_skew_ms`, which `media.segment` sets only when both tracks are in one
   segment. On a demuxed rung that is always `None`: no false alarm, and no measurement.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.analysis import av_pairing, layout
from app.analysis.rules import segments as segment_rules
from app.analysis.rules.base import StreamLayer
from app.config import Thresholds
from app.hls.playlist import parse_master
from app.media.ts import PTS_HZ
from tests.fixtures.synth import VariantSpec, render_master_playlist

T = Thresholds()
LAYER = StreamLayer.PLAYBACK
NOW = dt.datetime(2026, 9, 22, 10, 0, 0, tzinfo=dt.UTC)
MASTER_URL = "https://cdn.example/live/ch1/master.m3u8"


def _master(text: str):  # type: ignore[no-untyped-def]
    return parse_master(text, MASTER_URL)


def _demuxed_ladder() -> str:
    return render_master_playlist(
        [
            VariantSpec(name="v720", bandwidth=2_000_000, audio_group="aac", codecs="avc1.64001f"),
            VariantSpec(
                name="v1080",
                bandwidth=5_000_000,
                resolution="1920x1080",
                audio_group="aac",
                codecs="avc1.640028",
            ),
        ],
        audio_renditions=[("aac", "English", "audio/en.m3u8")],
    )


def _muxed_ladder() -> str:
    return render_master_playlist(
        [
            VariantSpec(name="v720", bandwidth=2_000_000),
            VariantSpec(name="v1080", bandwidth=5_000_000, resolution="1920x1080"),
        ]
    )


# ---------------------------------------------------------------------------
# Layout detection
# ---------------------------------------------------------------------------


def test_a_rung_pointing_at_an_audio_rendition_with_its_own_uri_is_demuxed() -> None:
    detected = layout.detect(_master(_demuxed_ladder()))

    for entry in detected.by_variant.values():
        assert entry.demuxed
        assert entry.audio_group == "aac"
        assert entry.audio_variants  # the rendition that carries the audio is named


def test_a_rung_with_no_audio_attribute_is_muxed() -> None:
    detected = layout.detect(_master(_muxed_ladder()))

    assert all(entry.muxed for entry in detected.by_variant.values())
    assert all(entry.audio_variants == () for entry in detected.by_variant.values())


def test_an_audio_entry_with_no_uri_leaves_the_rung_muxed() -> None:
    """RFC 8216 §4.3.4.2.1: an EXT-X-MEDIA entry without a URI describes audio carried in
    the video segments. The AUDIO attribute alone does not make a rung demuxed, and a rung
    read as demuxed here would silence AUD-003 on a channel that really is missing audio."""
    text = render_master_playlist(
        [VariantSpec(name="v720", bandwidth=2_000_000, audio_group="aac")],
        audio_renditions=[("aac", "English", "")],
    )

    detected = layout.detect(_master(text))

    entry = detected.by_variant["v720p@2000k"]
    assert entry.muxed
    assert entry.audio_group == "aac"
    assert entry.audio_variants == ()


def test_a_ladder_that_packages_rungs_both_ways_is_reported_as_mixed() -> None:
    text = render_master_playlist(
        [
            VariantSpec(name="v720", bandwidth=2_000_000, audio_group="aac"),
            VariantSpec(name="v1080", bandwidth=5_000_000, resolution="1920x1080"),
        ],
        audio_renditions=[("aac", "English", "audio/en.m3u8")],
    )

    detected = layout.detect(_master(text))

    assert detected.mixed
    assert detected.by_variant["v720p@2000k"].demuxed
    assert detected.by_variant["v1080p@5000k"].muxed


def test_the_layout_names_which_rungs_take_their_audio_from_each_rendition() -> None:
    detected = layout.detect(_master(_demuxed_ladder()))

    consumers = detected.audio_consumers
    assert len(consumers) == 1
    assert set(next(iter(consumers.values()))) == {"v720p@2000k", "v1080p@5000k"}


# ---------------------------------------------------------------------------
# AUD-003, which must keep firing where it is right
# ---------------------------------------------------------------------------


class _Analysis:
    """The handful of fields `_check_audio` reads, without building a real segment."""

    def __init__(self, *, has_video: bool = True, has_audio: bool = False, msn: int = 7) -> None:
        self.has_video = has_video
        self.has_audio = has_audio
        self.msn = msn
        self.audio_codec = "mp4a" if has_audio else ""


def _audio_findings(analysis: _Analysis, entry: layout.VariantLayout | None):  # type: ignore[no-untyped-def]
    return segment_rules._check_audio(
        analysis,  # type: ignore[arg-type]
        variant="v720p@2000k",
        layer=LAYER,
        at=NOW,
        evidence={"variant": "v720p@2000k", "msn": analysis.msn},
        is_audio_only=False,
        layout=entry,
    )


def test_a_demuxed_video_segment_with_no_audio_raises_nothing() -> None:
    """The false positive itself: this is what every CMAF video segment looks like."""
    entry = layout.detect(_master(_demuxed_ladder())).by_variant["v720p@2000k"]

    findings = _audio_findings(_Analysis(has_audio=False), entry)

    assert findings == []


def test_a_muxed_video_segment_with_no_audio_still_fires_aud_003() -> None:
    """The alarm that must survive the fix: this rung has nowhere else to carry its audio."""
    entry = layout.detect(_master(_muxed_ladder())).by_variant["v720p@2000k"]

    findings = _audio_findings(_Analysis(has_audio=False), entry)

    assert [f.rule.id for f in findings] == ["AUD-003"]


def test_a_rung_with_no_layout_supplied_is_judged_as_muxed() -> None:
    """A single-rendition channel has no master to read a layout off, and its one playlist
    is where its audio has to be. Callers that pass nothing keep today's behaviour."""
    findings = _audio_findings(_Analysis(has_audio=False), None)

    assert [f.rule.id for f in findings] == ["AUD-003"]


def test_a_demuxed_rung_that_also_carries_audio_is_reported_once() -> None:
    entry = layout.detect(_master(_demuxed_ladder())).by_variant["v720p@2000k"]

    first = _audio_findings(_Analysis(has_audio=True, msn=7), entry)
    second = _audio_findings(_Analysis(has_audio=True, msn=8), entry)

    assert [f.rule.id for f in first] == ["AUD-010"]
    assert second == [], "AUD-010 is a property of the rung, not of each segment"


def test_a_muxed_rung_carrying_audio_raises_nothing() -> None:
    entry = layout.detect(_master(_muxed_ladder())).by_variant["v720p@2000k"]

    assert _audio_findings(_Analysis(has_audio=True), entry) == []


def test_the_first_readable_segment_is_what_the_layout_records_as_observed() -> None:
    entry = layout.detect(_master(_demuxed_ladder())).by_variant["v720p@2000k"]

    _audio_findings(_Analysis(has_audio=False, msn=11), entry)
    _audio_findings(_Analysis(has_audio=True, msn=12), entry)

    assert entry.observed_audio is False
    assert entry.observed_at_msn == 11


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------


def _seg(variant: str, msn: int, start_s: float, duration_s: float = 6.0):  # type: ignore[no-untyped-def]
    return av_pairing.TrackSegment(
        variant=variant,
        msn=msn,
        uri=f"{variant}/{msn}.m4s",
        first_pts=int(start_s * PTS_HZ),
        last_pts=int((start_s + duration_s) * PTS_HZ),
        at=NOW,
    )


def _pairing(**kwargs: object) -> av_pairing.AvPairing:
    return av_pairing.AvPairing(
        video_variant="v720p@2000k",
        audio_variant="audio_en",
        **kwargs,  # type: ignore[arg-type]
    )


def test_pairs_are_matched_by_media_sequence_number() -> None:
    pairing = _pairing()
    for msn in (100, 101, 102):
        pairing.add_video(_seg("v720p@2000k", msn, msn * 6.0))
        pairing.add_audio(_seg("audio_en", msn, msn * 6.0))

    pairs = pairing.match()

    assert [(p.video.msn, p.audio.msn) for p in pairs] == [(100, 100), (101, 101), (102, 102)]
    assert all(p.match == av_pairing.MATCH_SEQUENCE for p in pairs)


def test_a_missing_audio_segment_does_not_shift_every_later_pair() -> None:
    """The drift-by-one bug. Pairing by position in a filtered list pairs video 102 with
    audio 103 once audio 101 is missed, and every pair after it is a segment out — which
    reads as a skew of exactly one segment duration on a stream that is in step."""
    pairing = _pairing()
    for msn in (100, 101, 102, 103):
        pairing.add_video(_seg("v720p@2000k", msn, msn * 6.0))
    for msn in (100, 102, 103):  # 101 was never sampled
        pairing.add_audio(_seg("audio_en", msn, msn * 6.0))

    pairs = pairing.match()

    assert [(p.video.msn, p.audio.msn) for p in pairs] == [(100, 100), (102, 102), (103, 103)]
    assert all(p.skew_ms == 0.0 for p in pairs)
    assert 101 in pairing.pending_video, "the unmatched video segment is held, not mispaired"


def test_a_pair_whose_audio_is_not_published_yet_is_held_not_measured() -> None:
    pairing = _pairing(defer_refreshes=2)
    pairing.add_video(_seg("v720p@2000k", 100, 600.0))

    assert pairing.match() == []
    assert pairing.expire() == 1  # refresh 1, still held
    assert pairing.expire() == 1  # refresh 2, still held

    # The audio segment arrives before patience runs out.
    pairing.add_audio(_seg("audio_en", 100, 600.0))
    pairs = pairing.match()

    assert [(p.video.msn, p.audio.msn) for p in pairs] == [(100, 100)]
    assert pairing.abandoned == 0


def test_a_pair_whose_audio_never_arrives_is_given_up_on_and_counted() -> None:
    pairing = _pairing(defer_refreshes=2)
    pairing.add_video(_seg("v720p@2000k", 100, 600.0))
    pairing.match()

    for _ in range(3):
        pairing.expire()
    assert pairing.match() == []

    assert pairing.abandoned == 1
    assert 100 not in pairing.pending_video


def test_two_playlists_numbering_from_different_bases_fall_back_to_time() -> None:
    """A shared number is evidence of nothing until it is checked against the clock."""
    pairing = _pairing()
    pairing.add_video(_seg("v720p@2000k", 100, 600.0))
    # The audio rendition counts from its own base: its segment 5 covers the same moment,
    # while its segment 100 is ten minutes later.
    pairing.add_audio(_seg("audio_en", 5, 600.0))
    pairing.add_audio(_seg("audio_en", 100, 1200.0))

    pairs = pairing.match()

    assert [(p.video.msn, p.audio.msn) for p in pairs] == [(100, 5)]
    assert pairs[0].match == av_pairing.MATCH_TIME
    assert pairing.shared_numbering is False


def test_the_fallback_to_time_matching_is_stated_once_as_info_006() -> None:
    """Once a collision has proved the bases differ, every later pair on the rung is matched
    by time — and the reader is told that once, not once per segment."""
    pairing = _pairing()
    # The collision that settles it: one number, two moments that do not overlap.
    pairing.add_video(_seg("v720p@2000k", 100, 600.0))
    pairing.add_audio(_seg("audio_en", 100, 1200.0))
    for index in range(3):
        pairing.add_audio(_seg("audio_en", 5 + index, 600.0 + index * 6))
        if index:
            pairing.add_video(_seg("v720p@2000k", 100 + index, 600.0 + index * 6))

    findings = segment_rules.check_cross_rendition_av(pairing, layer=LAYER, thresholds=T, at=NOW)

    assert [f.rule.id for f in findings] == ["INFO-006"], "stated once for the whole rung"
    assert findings[0].evidence[0]["matched_by"] == av_pairing.MATCH_TIME


def test_a_segment_with_no_decode_time_produces_no_pair() -> None:
    """An encrypted segment whose payload was never read carries no timestamp. It must
    contribute nothing rather than a pair of zeros that reads as perfect sync."""
    pairing = _pairing()
    pairing.add_video(
        av_pairing.TrackSegment(
            variant="v720p@2000k", msn=100, uri="a.m4s", first_pts=None, last_pts=None, at=NOW
        )
    )
    pairing.add_audio(_seg("audio_en", 100, 600.0))

    assert pairing.match() == []
    assert pairing.pending_video == {}


# ---------------------------------------------------------------------------
# The skew that pairing makes measurable
# ---------------------------------------------------------------------------


def test_skew_across_renditions_fires_av_001_and_names_both_segments() -> None:
    pairing = _pairing()
    pairing.add_video(_seg("v720p@2000k", 100, 600.0))
    pairing.add_audio(_seg("audio_en", 100, 600.4))  # 400 ms late, past av_skew_error_ms

    findings = segment_rules.check_cross_rendition_av(pairing, layer=LAYER, thresholds=T, at=NOW)

    assert [f.rule.id for f in findings] == ["AV-001"]
    evidence = findings[0].evidence[0]
    assert evidence["video_msn"] == 100
    assert evidence["audio_msn"] == 100
    assert evidence["audio_variant"] == "audio_en"
    assert evidence["matched_by"] == av_pairing.MATCH_SEQUENCE
    assert round(evidence["av_skew_ms"]) == 400


def test_skew_past_a_second_across_renditions_fires_av_005() -> None:
    pairing = _pairing()
    pairing.add_video(_seg("v720p@2000k", 100, 600.0))
    pairing.add_audio(_seg("audio_en", 100, 601.2))

    findings = segment_rules.check_cross_rendition_av(pairing, layer=LAYER, thresholds=T, at=NOW)

    assert [f.rule.id for f in findings] == ["AV-005"]


def test_a_rendition_pair_in_step_raises_nothing() -> None:
    pairing = _pairing()
    for msn in range(100, 105):
        pairing.add_video(_seg("v720p@2000k", msn, msn * 6.0))
        pairing.add_audio(_seg("audio_en", msn, msn * 6.0 + 0.01))

    assert segment_rules.check_cross_rendition_av(pairing, layer=LAYER, thresholds=T, at=NOW) == []


def test_audio_at_48000_and_video_at_90000_agree_in_seconds() -> None:
    """`media.segment` converts every track's `tfdt` with its own `mdhd` timescale onto the
    90 kHz grid before pairing sees it. This pins that the two arrive comparable: the same
    moment expressed in 48000ths and in 90000ths must read as no skew."""
    moment_s = 612.5
    video_ticks = round(moment_s * 90000)
    audio_ticks = round(moment_s * 48000)

    video_pts = int(video_ticks / 90000 * PTS_HZ)
    audio_pts = int(audio_ticks / 48000 * PTS_HZ)

    pairing = _pairing()
    pairing.add_video(
        av_pairing.TrackSegment(
            variant="v720p@2000k",
            msn=100,
            uri="v.m4s",
            first_pts=video_pts,
            last_pts=video_pts + int(6 * PTS_HZ),
            at=NOW,
        )
    )
    pairing.add_audio(
        av_pairing.TrackSegment(
            variant="audio_en",
            msn=100,
            uri="a.m4s",
            first_pts=audio_pts,
            last_pts=audio_pts + int(6 * PTS_HZ),
            at=NOW,
        )
    )

    pairs = pairing.match()

    assert len(pairs) == 1
    assert abs(pairs[0].skew_ms) < 1.0


# ---------------------------------------------------------------------------
# Coverage — AUD-006 and AUD-007, which could not fire before
# ---------------------------------------------------------------------------


def test_a_video_range_no_audio_segment_covers_fires_aud_006() -> None:
    findings = segment_rules.check_demuxed_audio_coverage(
        video_ranges=[(100, 600.0, 606.0), (101, 606.0, 612.0)],
        audio_ranges=[(100, 600.0, 606.0)],
        variant="v720p@2000k",
        layer=LAYER,
    )

    assert "AUD-006" in {f.rule.id for f in findings}


def test_audio_covering_every_video_range_raises_nothing() -> None:
    findings = segment_rules.check_demuxed_audio_coverage(
        video_ranges=[(100, 600.0, 606.0), (101, 606.0, 612.0)],
        audio_ranges=[(100, 600.0, 606.0), (101, 606.0, 612.0)],
        variant="v720p@2000k",
        layer=LAYER,
    )

    assert findings == []


def test_the_pairing_hands_the_coverage_check_both_sets_of_ranges() -> None:
    """The wiring that was missing: nothing ever called the coverage detector, so AUD-006
    and AUD-007 were declared in the catalogue and unreachable."""
    pairing = _pairing()
    for msn in (100, 101):
        pairing.add_video(_seg("v720p@2000k", msn, msn * 6.0))
    pairing.add_audio(_seg("audio_en", 100, 600.0))

    video_ranges, audio_ranges = pairing.ranges()

    assert [r[0] for r in video_ranges] == [100, 101]
    assert [r[0] for r in audio_ranges] == [100]

    findings = segment_rules.check_demuxed_audio_coverage(
        video_ranges=video_ranges,
        audio_ranges=audio_ranges,
        variant=pairing.video_variant,
        layer=LAYER,
    )
    assert "AUD-006" in {f.rule.id for f in findings}


def test_a_paired_video_segment_still_counts_towards_coverage() -> None:
    """`match()` empties `pending_video`; the coverage question is about every segment
    sampled, so it reads a separate index that pairing does not consume."""
    pairing = _pairing()
    pairing.add_video(_seg("v720p@2000k", 100, 600.0))
    pairing.add_audio(_seg("audio_en", 100, 600.0))
    pairing.match()

    video_ranges, _ = pairing.ranges()

    assert [r[0] for r in video_ranges] == [100]


@pytest.mark.parametrize("keep", [1, 3])
def test_the_indexes_are_bounded_so_a_long_session_does_not_grow_without_end(keep: int) -> None:
    pairing = _pairing()
    for msn in range(100, 110):
        pairing.add_video(_seg("v720p@2000k", msn, msn * 6.0))
        pairing.add_audio(_seg("audio_en", msn, msn * 6.0))
    pairing.match()

    pairing.trim(keep=keep)

    assert len(pairing.audio_by_msn) == keep
    assert len(pairing.seen_video) == keep


def test_an_audio_segment_that_simply_arrived_late_is_not_called_a_numbering_mismatch() -> None:
    """The deferral running out means the segment was slow, not that the numbers disagree.

    Treating the late arrival as a time match would report INFO-006 — "these two playlists
    number from different bases" — against a ladder that numbers them identically, which is
    a false statement in a report.
    """
    pairing = _pairing(defer_refreshes=1)
    pairing.add_video(_seg("v720p@2000k", 100, 600.0))
    for _ in range(3):
        pairing.expire()

    # It turns up after the wait was over, under the number it was always going to have.
    pairing.add_audio(_seg("audio_en", 100, 600.0))
    pairs = pairing.match()

    assert [(p.video.msn, p.audio.msn) for p in pairs] == [(100, 100)]
    assert pairs[0].match == av_pairing.MATCH_SEQUENCE
    assert pairing.shared_numbering is True
    assert (
        segment_rules.check_cross_rendition_av(_pairing(), layer=LAYER, thresholds=T, at=NOW) == []
    )


# ---------------------------------------------------------------------------
# The mispairing that reached the deployment
# ---------------------------------------------------------------------------
#
# Reported from a live run:
#
#   AV-005  Audio segment 6394313 on audio_eng starts -5991 ms from video segment
#           6394314 on v1080p@5418k, against a critical threshold of 1000 ms.
#
# A skew of 5991 ms on a six-second ladder is one segment, which is the signature of a
# mispairing and not of anything a packager did. Two defects produced it: the audio segment
# for 6394314 had not been sampled when the deferral ran out, and the fallback then accepted
# audio 6394313 because it overlapped video 6394314 by the nine milliseconds two adjacent
# segments share at their boundary.


def _ladder(count: int, *, duration: float = 6.0, first_msn: int = 6394310):
    """Video and audio renditions in step, numbered identically, as the live channel is."""
    video = [
        _seg("v1080p@5418k", first_msn + i, (first_msn + i) * duration, duration)
        for i in range(count)
    ]
    audio = [
        _seg("audio_eng", first_msn + i, (first_msn + i) * duration, duration) for i in range(count)
    ]
    return video, audio


def test_the_segment_before_the_right_one_is_not_accepted_as_a_pair() -> None:
    """The exact shape of the live false positive, reduced to the two segments involved."""
    pairing = av_pairing.AvPairing(video_variant="v1080p@5418k", audio_variant="audio_eng")
    video, audio = _ladder(5)

    # Everything the audio rendition published except the one that matches.
    for segment in audio:
        if segment.msn != 6394314:
            pairing.add_audio(segment)
    pairing.add_video(next(v for v in video if v.msn == 6394314))

    for _ in range(5):
        pairing.expire()
    pairs = pairing.match()

    assert pairs == [], "video 6394314 was paired with an audio segment that is not its own"
    assert pairing.abandoned == 1


def test_a_whole_ladder_in_step_raises_no_skew_finding_when_one_audio_segment_is_missed() -> None:
    """End of the same story: the run carries on, the other pairs are measured, and the one
    video segment with no audio of its own produces no finding at all."""
    pairing = av_pairing.AvPairing(video_variant="v1080p@5418k", audio_variant="audio_eng")
    video, audio = _ladder(5)
    for segment in video:
        pairing.add_video(segment)
    for segment in audio:
        if segment.msn != 6394314:
            pairing.add_audio(segment)

    for _ in range(5):
        pairing.expire()
    findings = segment_rules.check_cross_rendition_av(pairing, layer=LAYER, thresholds=T, at=NOW)

    assert findings == [], [f.rule.id for f in findings]
    assert pairing.paired == 4
    assert pairing.abandoned == 1


def test_a_real_skew_inside_a_segment_is_still_measured() -> None:
    """The alarm that must survive: a genuine 400 ms offset shares most of the range, so the
    pair is sound and AV-001 fires on it."""
    pairing = av_pairing.AvPairing(video_variant="v1080p@5418k", audio_variant="audio_eng")
    pairing.add_video(_seg("v1080p@5418k", 6394313, 6394313 * 6.0))
    pairing.add_audio(_seg("audio_eng", 6394313, 6394313 * 6.0 + 0.4))

    findings = segment_rules.check_cross_rendition_av(pairing, layer=LAYER, thresholds=T, at=NOW)

    assert [f.rule.id for f in findings] == ["AV-001"]
    assert round(findings[0].evidence[0]["av_skew_ms"]) == 400


def test_a_missing_number_is_not_taken_as_proof_that_the_bases_differ() -> None:
    """An audio rendition polled a little behind its video rung is ordinary. Reading that as
    "these playlists number from different bases" raised INFO-006 on a healthy ladder and
    licensed the time fallback that produced the mispairing."""
    pairing = av_pairing.AvPairing(video_variant="v1080p@5418k", audio_variant="audio_eng")
    video, _audio = _ladder(3)
    for segment in video:
        pairing.add_video(segment)
    for _ in range(5):
        pairing.expire()
    pairing.match()

    assert pairing.shared_numbering is not False


def test_bases_that_really_do_differ_are_still_found_and_still_matched_by_time() -> None:
    """The fallback has to keep working where it is right: one number, two moments."""
    pairing = av_pairing.AvPairing(video_variant="v1080p@5418k", audio_variant="audio_eng")
    pairing.add_video(_seg("v1080p@5418k", 100, 600.0))
    pairing.add_audio(_seg("audio_eng", 100, 1200.0))  # same number, ten minutes later
    pairing.add_audio(_seg("audio_eng", 5, 600.0))  # the one that covers it

    findings = segment_rules.check_cross_rendition_av(pairing, layer=LAYER, thresholds=T, at=NOW)

    assert pairing.shared_numbering is False
    assert [f.rule.id for f in findings] == ["INFO-006"]
    assert findings[0].evidence[0]["audio_msn"] == 5

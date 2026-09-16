"""The Virtual Player Buffer against Plus Player's documented buffering configuration.

The player bounds its queue by a byte cap and a time cap at once, and the smaller binds.
A time-only model held sixty seconds where the device holds three, which hid rebuffering on
exactly the rungs most at risk of it. These tests hold the model to the documented numbers.

Source: Samsung TV Plus Player Logic, section 3, "Buffering configuration for streaming".

    | Content type | Total       | Startup     | Resume/Seek  |
    | FHD          | 3 MB / 15 s | 1 MB / 5 s  | 2 MB / 10 s  |
    | UHD/8K       | 60 MB / 15 s| 20 MB / 5 s | 40 MB / 10 s |

    Multiqueue: low 1%, startup high 33%, resume/seek high 66%.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.analysis import vpb
from app.analysis.rules.base import StreamLayer
from app.config import Thresholds

T = Thresholds()
# The byte caps are not applied by default; these tests exercise them deliberately.
BYTES = Thresholds(vpb_apply_byte_caps=True)
START = dt.datetime(2026, 9, 16, 12, 0, 0, tzinfo=dt.UTC)
MB = 1_000_000


def at(seconds: float) -> dt.datetime:
    return START + dt.timedelta(seconds=seconds)


def delivery(
    msn: int, completed_s: float, *, duration: float = 6.0, bitrate_bps: float = 0.0
) -> vpb.SegmentDelivery:
    """One segment, sized from the rate it was encoded at."""
    return vpb.SegmentDelivery(
        msn=msn,
        completed_at=at(completed_s),
        duration_s=duration,
        bytes=int(bitrate_bps * duration / 8),
    )


# -- the profile -------------------------------------------------------------


@pytest.mark.parametrize("height", [360, 720, 1080])
def test_a_ladder_topping_out_below_2160p_is_an_fhd_channel(height: int) -> None:
    profile = vpb.profile_for(height, T)
    assert profile.name == "FHD"
    assert (profile.total_bytes, profile.total_s) == (3 * MB, 15.0)


@pytest.mark.parametrize("height", [2160, 4320])
def test_a_ladder_reaching_2160p_is_a_uhd_channel(height: int) -> None:
    profile = vpb.profile_for(height, T)
    assert profile.name == "UHD"
    assert (profile.total_bytes, profile.total_s) == (60 * MB, 15.0)


def test_a_ladder_that_declares_no_resolution_falls_back_to_fhd() -> None:
    """The default configuration, which is what the document calls the FHD row."""
    assert vpb.profile_for(None, T).name == "FHD"


@pytest.mark.parametrize(
    ("height", "startup_mb", "startup_s", "resume_mb", "resume_s"),
    [(1080, 1.0, 5.0, 2.0, 10.0), (2160, 20.0, 5.0, 40.0, 10.0)],
)
def test_the_multiqueue_percentages_reproduce_the_documented_watermarks(
    height: int, startup_mb: float, startup_s: float, resume_mb: float, resume_s: float
) -> None:
    """
    The two tables in the document agree, and the model uses the mechanism behind them.

    Startup and resume are stored as the 33% and 66% multiqueue high thresholds rather than
    as fixed sizes; this asserts they land on the sizes the buffering table states.
    """
    profile = vpb.profile_for(height, T)
    got_startup_bytes, got_startup_s = profile.startup
    got_resume_bytes, got_resume_s = profile.resume

    # The document prints whole figures: 33% of 60 MB is 19.8, which it states as 20 MB.
    assert round(got_startup_bytes / MB) == startup_mb
    assert round(got_startup_s) == startup_s
    assert round(got_resume_bytes / MB) == resume_mb
    assert round(got_resume_s) == resume_s
    # The low watermark is 1% of the total, which is where playback underruns.
    assert profile.low_s == pytest.approx(0.15, abs=0.01)


@pytest.mark.parametrize(
    ("bitrate", "expected_s"),
    [(1_249_336, 15.0), (2_393_072, 10.0), (4_108_676, 5.8), (7_539_884, 3.2)],
)
def test_the_byte_cap_is_what_binds_on_a_high_rung(bitrate: int, expected_s: float) -> None:
    """
    3 MB is 15 s of 1.6 Mbit/s media and 3.2 s of 7.5 Mbit/s media.

    This is the whole reason the byte cap matters: the rung most likely to rebuffer is the
    one whose buffer the time cap describes least well.
    """
    assert vpb.profile_for(1080, BYTES).seconds_at(bitrate) == pytest.approx(expected_s, abs=0.1)


# -- the queue ---------------------------------------------------------------


def test_a_high_bitrate_rung_fills_its_queue_on_bytes_long_before_time() -> None:
    """Six 7.5 Mbit/s segments are 36 s of media and far past 3 MB."""
    rate = 7_539_884
    deliveries = [delivery(n, 0.5 + n * 0.5, bitrate_bps=rate) for n in range(6)]
    result = vpb.run(
        "v1080p", deliveries, target_duration=6.0, thresholds=BYTES, end_at=at(4), top_height=1080
    )

    assert result.profile["name"] == "FHD"
    # The queue could never hold 36 s; the cap was reached and the rest was never asked for.
    assert result.buffer_too_long_events
    held = max(point.level_s for point in result.points)
    assert held <= vpb.profile_for(1080, BYTES).seconds_at(rate) + 0.1


def test_the_same_delivery_pattern_holds_far_more_on_a_uhd_channel() -> None:
    """
    Which cap binds is what separates the two profiles.

    At 7.5 Mbit/s, FHD's 3 MB runs out after about three seconds while UHD's 60 MB is 64 s
    of media, so UHD fills to its 15 s time cap instead — five times the media held.
    """
    rate = 7_539_884
    deliveries = [delivery(n, 0.5 + n * 0.5, bitrate_bps=rate) for n in range(6)]
    fhd = vpb.run(
        "v1080p", deliveries, target_duration=6.0, thresholds=BYTES, end_at=at(4), top_height=1080
    )
    uhd = vpb.run(
        "v2160p", deliveries, target_duration=6.0, thresholds=BYTES, end_at=at(4), top_height=2160
    )

    fhd_held = max(point.level_s for point in fhd.points)
    uhd_held = max(point.level_s for point in uhd.points)

    assert fhd.profile["name"] == "FHD"
    assert uhd.profile["name"] == "UHD"
    # FHD is stopped by its byte cap well short of the shared 15 s time cap; UHD reaches it.
    assert fhd_held < 4.0
    assert uhd_held == pytest.approx(15.0, abs=0.1)


def test_media_played_takes_its_bytes_out_of_the_queue_with_it() -> None:
    """The byte level has to fall as playback consumes media, or the cap jams shut."""
    rate = 4_000_000
    buffer = vpb.VirtualPlayerBuffer(
        "v720p", target_duration=6.0, thresholds=BYTES, top_height=1080
    )
    buffer.feed(delivery(0, 0.5, bitrate_bps=rate))
    filled = buffer.level_bytes
    assert filled > 0

    # Four seconds of playback, then another segment that the freed room accepts.
    buffer.feed(delivery(1, 4.5, bitrate_bps=rate))
    assert buffer.level_bytes < filled * 2
    assert buffer.level_s > 0


def test_a_segment_that_reports_no_size_leaves_the_byte_cap_unable_to_bind() -> None:
    """Aging replays historical deliveries that may carry no measured size."""
    deliveries = [vpb.SegmentDelivery(msn=n, completed_at=at(n), duration_s=6.0) for n in range(4)]
    result = vpb.run("v720p", deliveries, target_duration=6.0, thresholds=T, top_height=1080)
    assert result.started_playback is True


# -- underrun and resume -----------------------------------------------------


def test_playback_underruns_at_the_low_watermark_rather_than_at_zero() -> None:
    """The player stops at 1% of the queue, which for FHD is 0.15 s."""
    result = vpb.run(
        "v720p",
        [delivery(0, 0.5, duration=6.0)],
        target_duration=6.0,
        thresholds=T,
        end_at=at(30),
        top_height=1080,
    )
    assert result.started_playback is True
    assert result.stall_s > 0
    assert result.stalls


def test_a_resume_waits_for_the_higher_watermark_than_a_start_did() -> None:
    """
    Startup fills to 33% and a resume to 66%.

    A stream that trickles one short segment at a time starts, underruns, and then needs
    more media to come back than it needed to begin with.
    """
    profile = vpb.profile_for(1080, T)
    startup_s = profile.startup[1]
    resume_s = profile.resume[1]
    assert resume_s > startup_s

    buffer = vpb.VirtualPlayerBuffer("v720p", target_duration=6.0, thresholds=T, top_height=1080)
    buffer.feed(delivery(0, 0.0, duration=startup_s + 0.2))
    assert buffer.state is vpb.BufferState.PLAYING

    # Play it all out, so the queue underruns.
    buffer.feed(delivery(1, 30.0, duration=0.0))
    assert buffer.state is vpb.BufferState.REBUFFERING

    # Media past the startup mark but short of the resume mark does not restart playback.
    buffer.feed(delivery(2, 30.5, duration=startup_s + 0.2))
    assert buffer.state is vpb.BufferState.REBUFFERING

    buffer.feed(delivery(3, 31.0, duration=resume_s - startup_s))
    assert buffer.state is vpb.BufferState.PLAYING


# -- what the report carries -------------------------------------------------


def test_the_replay_states_which_player_queue_it_modelled() -> None:
    """A rebuffer ratio means nothing without the buffer it was measured against."""
    result = vpb.run(
        "v1080p",
        [delivery(0, 0.5, bitrate_bps=7_539_884)],
        target_duration=6.0,
        thresholds=T,
        top_height=1080,
    )
    profile = result.as_dict()["profile"]
    assert profile["name"] == "FHD"
    assert profile["total_mb"] == 3.0
    assert profile["total_s"] == 15.0
    assert profile["startup_s"] == pytest.approx(4.95, abs=0.01)
    assert profile["resume_s"] == pytest.approx(9.9, abs=0.01)


def test_a_full_queue_finding_names_only_the_caps_that_are_in_force() -> None:
    """The finding states the queue it measured, so a reader knows which cap was reached."""
    rate = 7_539_884
    deliveries = [delivery(n, 0.5 + n * 0.5, bitrate_bps=rate) for n in range(6)]

    def detail(thresholds: Thresholds) -> str:
        result = vpb.run(
            "v1080p",
            deliveries,
            target_duration=6.0,
            thresholds=thresholds,
            end_at=at(4),
            top_height=1080,
        )
        findings = vpb.findings_for(result, layer=StreamLayer.PLAYBACK, thresholds=thresholds)
        full = next(f for f in findings if f.rule.id == "VPB-003")
        return full.detail

    applied = detail(BYTES)
    assert "3 MB or 15 s" in applied

    time_only = detail(T)
    assert "cap of 15 s" in time_only
    assert "MB" not in time_only


def test_the_byte_caps_are_not_applied_until_the_player_team_confirms_them() -> None:
    """
    Taken literally the byte cap says a healthy channel underruns every segment.

    3 MB is 3.2 s at 7.5 Mbit/s, shorter than one 6 s segment, so an on-time 1080p channel
    would rebuffer continuously — which devices do not do. The document lists `OutputMgr` as
    a separate queue for downloaded segments, so the byte figures size the decoder-side
    multiqueue rather than the buffer that governs rebuffering. They are implemented and
    configurable, and off until that is settled.
    """
    assert Thresholds().vpb_apply_byte_caps is False

    # A healthy live channel: six seconds of media arriving every five and a half seconds,
    # comfortably ahead of playback.
    rate = 7_539_884
    deliveries = [delivery(n, 0.5 + n * 5.5, bitrate_bps=rate) for n in range(10)]
    default = vpb.run(
        "v1080p", deliveries, target_duration=6.0, thresholds=T, end_at=at(50), top_height=1080
    )
    literal = vpb.run(
        "v1080p", deliveries, target_duration=6.0, thresholds=BYTES, end_at=at(50), top_height=1080
    )

    # On-time delivery, so the applied model reports a clean stream.
    assert default.stall_s == pytest.approx(0.0, abs=0.01)
    assert default.profile["byte_caps_applied"] is False
    # The literal reading calls the same clean stream a continuous rebuffer.
    assert literal.stall_s > 10
    assert literal.profile["byte_caps_applied"] is True

"""Pairing a demuxed rung's video segments with its audio rendition's segments.

On a muxed rung the A/V skew is read off one segment, because both tracks are in it. On a
demuxed rung there is no such segment: the video and the audio are separate renditions,
polled independently, sampled independently, and `SegmentAnalysis.av_skew_ms` is `None` for
every one of them. `AV-001` to `AV-005` were therefore never measured on a demuxed channel —
no false alarm, and no measurement either.

Pairing is by **absolute media sequence number** — `EXT-X-MEDIA-SEQUENCE` plus the index in
the playlist — and never by position in a list of samples. A sampled list is a filtered list:
one missed audio segment shifts every later position by one, and from then on every pair is
a segment out, which reads as a skew of exactly one segment duration that grows as nothing.
The number survives that; the position does not.

Three things then have to hold before a pair is measured:

* The two segments must overlap on the timeline. A shared number is only evidence that the
  two playlists count from the same base, and they do not have to: a packager is free to
  start the audio rendition at its own `EXT-X-MEDIA-SEQUENCE`. The first pair that does not
  overlap settles that, and pairing falls back to matching by time, which is reported so a
  reader knows which of the two was used.
* The audio segment must exist yet. Renditions are polled on their own schedules, so a video
  segment routinely arrives before the audio segment covering it is published. Such a pair is
  held for `av_pair_defer_refreshes` refreshes rather than measured against the closest
  audio segment that happens to be there.
* Both segments must carry a decode time. An encrypted segment whose payload was not read
  carries none, and produces no pair rather than a pair of zeros.

Timestamps come from `tfdt` converted with each track's **own** `mdhd` timescale — video at
90000 and audio at 48000 are different counts of the same second. `media.segment` already
does that conversion and stores both tracks on the 90 kHz `ts.PTS_HZ` grid, so this module
compares what it produced rather than re-deriving it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from app.media import ts

# How a pair was matched. `sequence` is the numbering both playlists share; `time` is the
# fallback for two playlists that do not share one.
MATCH_SEQUENCE = "sequence"
MATCH_TIME = "time"


@dataclass(slots=True)
class TrackSegment:
    """One sampled segment of one rendition, reduced to what pairing needs."""

    variant: str
    msn: int
    uri: str
    first_pts: int | None
    last_pts: int | None
    at: dt.datetime

    @property
    def start_s(self) -> float | None:
        return None if self.first_pts is None else self.first_pts / ts.PTS_HZ

    @property
    def end_s(self) -> float | None:
        if self.last_pts is None:
            return self.start_s
        return self.last_pts / ts.PTS_HZ

    @property
    def usable(self) -> bool:
        return self.msn >= 0 and self.first_pts is not None


@dataclass(slots=True)
class MatchedPair:
    """A video segment and the audio segment covering the same moment."""

    video: TrackSegment
    audio: TrackSegment
    match: str

    @property
    def skew_ms(self) -> float:
        """How far the audio starts from the video, in milliseconds, audio-minus-video."""
        assert self.video.first_pts is not None and self.audio.first_pts is not None
        return ts.pts_diff(self.audio.first_pts, self.video.first_pts) / ts.PTS_HZ * 1000.0

    def evidence(self) -> dict[str, object]:
        """The pair itself, so a skew finding names the two segments it was measured on."""
        return {
            "video_variant": self.video.variant,
            "video_msn": self.video.msn,
            "video_uri": self.video.uri,
            "audio_variant": self.audio.variant,
            "audio_msn": self.audio.msn,
            "audio_uri": self.audio.uri,
            "matched_by": self.match,
        }


@dataclass
class AvPairing:
    """Per (video rung, audio rendition) state, carried for the life of the session."""

    video_variant: str
    audio_variant: str
    defer_refreshes: int = 3
    overlap_tolerance_s: float = 1.0

    # Sampled segments held until they are paired or given up on, by media sequence number.
    pending_video: dict[int, TrackSegment] = field(default_factory=dict)
    # Every video segment sampled on this rung, kept whether it paired or not: the coverage
    # check asks which video ranges no audio covers, and a paired segment is still one of
    # them. Bounded by `trim()`, like the audio index.
    seen_video: dict[int, TrackSegment] = field(default_factory=dict)
    audio_by_msn: dict[int, TrackSegment] = field(default_factory=dict)
    # video msn -> how many refreshes it has waited for its audio segment.
    waited: dict[int, int] = field(default_factory=dict)

    # None until a pair settles it: True when both playlists count from the same base.
    shared_numbering: bool | None = None
    # Stated once, not once per pair.
    fallback_reported: bool = False
    # Video segments given up on, which is what `AUD-006` reports on its own evidence.
    abandoned: int = 0
    paired: int = 0

    # -- collection ------------------------------------------------------------

    def add_video(self, segment: TrackSegment) -> None:
        if not segment.usable:
            return
        if segment.msn in self.seen_video:
            return
        self.seen_video[segment.msn] = segment
        self.pending_video[segment.msn] = segment
        self.waited[segment.msn] = 0

    def add_audio(self, segment: TrackSegment) -> None:
        if not segment.usable:
            return
        self.audio_by_msn.setdefault(segment.msn, segment)

    # -- matching --------------------------------------------------------------

    def _overlaps(self, video: TrackSegment, audio: TrackSegment) -> bool:
        v_start, v_end = video.start_s, video.end_s
        a_start, a_end = audio.start_s, audio.end_s
        if v_start is None or v_end is None or a_start is None or a_end is None:
            return False
        return a_start < v_end + self.overlap_tolerance_s and (
            a_end > v_start - self.overlap_tolerance_s
        )

    def _best_by_time(self, video: TrackSegment) -> TrackSegment | None:
        """The audio segment sharing the most of this video segment's range."""
        v_start, v_end = video.start_s, video.end_s
        if v_start is None or v_end is None:
            return None
        best: TrackSegment | None = None
        best_overlap = 0.0
        for audio in self.audio_by_msn.values():
            a_start, a_end = audio.start_s, audio.end_s
            if a_start is None or a_end is None:
                continue
            overlap = min(v_end, a_end) - max(v_start, a_start)
            if overlap > best_overlap:
                best, best_overlap = audio, overlap
        return best

    def match(self) -> list[MatchedPair]:
        """Every pair that can be measured now.

        Called once per sampled segment, so it costs one pass over what is still pending. A
        video segment whose audio has neither arrived nor run out of patience stays pending;
        one that has waited long enough and still matches nothing is given up on and counted,
        which is what `AUD-006` reports on its own evidence.
        """
        pairs: list[MatchedPair] = []
        for msn in sorted(self.pending_video):
            video = self.pending_video[msn]
            out_of_patience = self.waited.get(msn, 0) > self.defer_refreshes
            pair = self._match_one(video, out_of_patience=out_of_patience)
            if pair is None:
                if out_of_patience:
                    del self.pending_video[msn]
                    self.waited.pop(msn, None)
                    self.abandoned += 1
                continue
            pairs.append(pair)
            del self.pending_video[msn]
            self.waited.pop(msn, None)
            self.paired += 1
        return pairs

    def _match_one(self, video: TrackSegment, *, out_of_patience: bool) -> MatchedPair | None:
        if self.shared_numbering is not False:
            candidate = self.audio_by_msn.get(video.msn)
            if candidate is not None:
                if self._overlaps(video, candidate):
                    self.shared_numbering = True
                    return MatchedPair(video=video, audio=candidate, match=MATCH_SEQUENCE)
                # Same number, different moment: the two playlists number from different
                # bases. Settled once, for the life of the session.
                self.shared_numbering = False
            elif not out_of_patience:
                # The audio segment for this number is not published yet. Waiting is what
                # `expire()` counts; nothing is measured against a different segment until
                # the deferral runs out.
                return None

        # Either the numbers disagree, or the number never arrived and the wait is over.
        # Overlapping decode times are the looser match, and the one the caller reports.
        by_time = self._best_by_time(video)
        if by_time is None:
            return None
        if self.shared_numbering is None:
            # A time match found where the numbered one never appeared is itself the proof
            # that the two playlists do not count from the same base.
            self.shared_numbering = False
        return MatchedPair(video=video, audio=by_time, match=MATCH_TIME)

    def expire(self) -> int:
        """Advance the deferral clock one refresh, and say how many segments are still held.

        A caller runs this once per playlist refresh of the audio rendition, because that is
        the event that could have published the missing segment. Giving up belongs to
        `match()`, which is the only place that knows whether a late arrival still pairs.
        """
        for msn in self.pending_video:
            self.waited[msn] = self.waited.get(msn, 0) + 1
        return len(self.pending_video)

    def trim(self, keep: int = 200) -> None:
        """Bound both indexes. A live session runs for hours; the live window does not.

        Only the oldest entries go, and `pending_video` is never trimmed here: a segment
        still waiting for its audio is given up on by `expire()`, which counts it, rather
        than dropped silently.
        """
        for index in (self.audio_by_msn, self.seen_video):
            if len(index) <= keep:
                continue
            for msn in sorted(index)[: len(index) - keep]:
                del index[msn]

    def ranges(self) -> tuple[list[tuple[int, float, float]], list[tuple[int, float, float]]]:
        """`(video, audio)` ranges on the shared timeline, for the coverage check.

        Only segments carrying a decode time appear; an encrypted segment whose payload was
        never read contributes nothing rather than a range of zeros.
        """
        video = [
            (seg.msn, seg.start_s, seg.end_s)
            for seg in sorted(self.seen_video.values(), key=lambda s: s.msn)
            if seg.start_s is not None and seg.end_s is not None
        ]
        audio = [
            (seg.msn, seg.start_s, seg.end_s)
            for seg in sorted(self.audio_by_msn.values(), key=lambda s: s.msn)
            if seg.start_s is not None and seg.end_s is not None
        ]
        return video, audio

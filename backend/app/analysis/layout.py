"""Whether a rung carries its audio in its own segments or in a separate rendition.

The ladder already says which it is, and the analyzer already computed it once — for
`MST-012` — and threw it away. Nothing downstream knew, so `AUD-003` ("Muxed segment carries
video and no audio elementary stream") fired CRITICAL on every video segment of every
demuxed channel, which is what a demuxed video segment is supposed to look like.

Layout is declared in the master and observed in the segments, and the two can disagree:

* A variant with no `AUDIO` attribute is muxed — its own segments carry the audio.
* A variant whose `AUDIO` group resolves to an `EXT-X-MEDIA:TYPE=AUDIO` entry **with a
  `URI`** is demuxed — the audio is a separate rendition with its own segments.
* A variant whose `AUDIO` group resolves only to entries with no `URI` is muxed. RFC 8216
  §4.3.4.2.1 uses exactly that spelling for audio that rides in the video segments, so the
  attribute alone does not make a rung demuxed.

The observed layout is what the init segment and the segment's own boxes carry. It never
relaxes `AUD-003`: a rung the ladder declares muxed that produces video with no audio is
silent, and that is the finding, not a layout correction. It is used in the other direction,
where a rung points at an audio rendition *and* carries audio itself, which `AUD-010`
reports — two audio tracks for one rung, and nothing in the manifest says which one plays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.hls.playlist import MasterPlaylist


@dataclass(slots=True)
class VariantLayout:
    """How one rung is packaged, as declared and as measured."""

    variant: str
    # Declared: the ladder points this rung at an audio rendition that has its own segments.
    demuxed: bool
    audio_group: str | None = None
    # The rendition ids serving this rung's audio, in the order the master lists them.
    audio_variants: tuple[str, ...] = ()
    # From CODECS on the variant, split into what the video and the audio parts declare.
    declared_codecs: tuple[str, ...] = ()
    # Set the first time a segment of this rung is read; None until then.
    observed_audio: bool | None = None
    observed_video: bool | None = None
    observed_at_msn: int | None = None
    # AUD-010 is stated once per rung, not once per segment.
    disagreement_reported: bool = False

    @property
    def muxed(self) -> bool:
        """True when this rung's own segments are where its audio is supposed to be."""
        return not self.demuxed

    @property
    def declares_audio_codec(self) -> bool:
        """Whether CODECS names an audio codec, which a demuxed video rung usually omits."""
        return any(_is_audio_codec(codec) for codec in self.declared_codecs)

    def observe(self, *, has_audio: bool, has_video: bool, msn: int) -> None:
        """Record what the first readable segment of this rung actually carried."""
        if self.observed_audio is not None:
            return
        self.observed_audio = has_audio
        self.observed_video = has_video
        self.observed_at_msn = msn

    def as_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "layout": "demuxed" if self.demuxed else "muxed",
            "audio_group": self.audio_group,
            "audio_variants": list(self.audio_variants),
            "declared_codecs": list(self.declared_codecs),
            "observed_audio": self.observed_audio,
            "observed_video": self.observed_video,
            "observed_at_msn": self.observed_at_msn,
        }


@dataclass(slots=True)
class LadderLayout:
    """Every rung's layout, plus the audio renditions they point at."""

    by_variant: dict[str, VariantLayout] = field(default_factory=dict)
    # rendition id -> the video rungs that take their audio from it.
    audio_consumers: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def get(self, variant: str) -> VariantLayout | None:
        return self.by_variant.get(variant)

    @property
    def mixed(self) -> bool:
        """True when the ladder packages some rungs muxed and others demuxed."""
        layouts = {layout.demuxed for layout in self.by_variant.values()}
        return len(layouts) > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "variants": [layout.as_dict() for layout in self.by_variant.values()],
            "mixed": self.mixed,
        }


# The audio codecs a CODECS attribute can name. A four-character code is matched in full so
# `avc1` and `ac-3` are never confused by a prefix test.
_AUDIO_CODEC_PREFIXES = ("mp4a", "ac-3", "ec-3", "ac-4", "opus", "fLaC", "alac", "dtsc", "dtse")


def _is_audio_codec(codec: str) -> bool:
    return codec.strip().lower().split(".")[0] in _AUDIO_CODEC_PREFIXES


def detect(master: MasterPlaylist) -> LadderLayout:
    """Read each rung's layout off the master playlist.

    Only the master is needed: `AUDIO` on the variant and `URI` on the `EXT-X-MEDIA` entry
    are what decide it, and both are parsed already.
    """
    layout = LadderLayout()
    consumers: dict[str, list[str]] = {}

    for variant in master.variants:
        group = variant.audio_group
        # An entry with no URI declares audio that rides in the video segments, so it does
        # not make the rung demuxed. Only a rendition with segments of its own does.
        renditions = [r for r in master.audio_renditions(group) if r.resolved_uri] if group else []
        codecs = tuple(c for c in (variant.codecs or "").split(",") if c.strip())
        entry = VariantLayout(
            variant=variant.variant_id,
            demuxed=bool(renditions),
            audio_group=group,
            audio_variants=tuple(r.variant_id for r in renditions),
            declared_codecs=codecs,
        )
        layout.by_variant[variant.variant_id] = entry
        for rendition in renditions:
            consumers.setdefault(rendition.variant_id, []).append(variant.variant_id)

    layout.audio_consumers = {k: tuple(v) for k, v in consumers.items()}
    return layout

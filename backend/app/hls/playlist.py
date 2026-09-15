"""HLS playlist parsing.

The parser keeps the raw text and the 1-based line number of every tag, because findings
quote the exact line that proves them. Values are left as written so a rule can report
"declared vs measured" without the parser having normalised the declaration away.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.hls.uri import resolve

_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)=("[^"]*"|[^,]*)')


def parse_attributes(value: str) -> dict[str, str]:
    """Parse an HLS attribute list, keeping values verbatim minus surrounding quotes."""
    attrs: dict[str, str] = {}
    for key, raw in _ATTR_RE.findall(value):
        attrs[key.upper()] = raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw
    return attrs


def _f(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


@dataclass(slots=True)
class Line:
    number: int
    text: str


@dataclass(slots=True)
class Variant:
    """One `EXT-X-STREAM-INF` entry."""

    uri: str
    resolved_uri: str
    attrs: dict[str, str]
    line: Line
    index: int

    @property
    def bandwidth(self) -> int | None:
        return parse_int(self.attrs.get("BANDWIDTH"))

    @property
    def average_bandwidth(self) -> int | None:
        return parse_int(self.attrs.get("AVERAGE-BANDWIDTH"))

    @property
    def codecs(self) -> str | None:
        return self.attrs.get("CODECS")

    @property
    def resolution(self) -> str | None:
        return self.attrs.get("RESOLUTION")

    @property
    def width(self) -> int | None:
        res = self.resolution
        return parse_int(res.split("x")[0]) if res and "x" in res else None

    @property
    def height(self) -> int | None:
        res = self.resolution
        return parse_int(res.split("x")[1]) if res and "x" in res else None

    @property
    def frame_rate(self) -> float | None:
        return _f(self.attrs.get("FRAME-RATE"))

    @property
    def audio_group(self) -> str | None:
        return self.attrs.get("AUDIO")

    @property
    def subtitles_group(self) -> str | None:
        return self.attrs.get("SUBTITLES")

    @property
    def closed_captions(self) -> str | None:
        return self.attrs.get("CLOSED-CAPTIONS")

    @property
    def video_range(self) -> str:
        return self.attrs.get("VIDEO-RANGE", "SDR")

    @property
    def supplemental_codecs(self) -> str | None:
        return self.attrs.get("SUPPLEMENTAL-CODECS")

    @property
    def variant_id(self) -> str:
        if self.resolution:
            return f"v{self.height}p@{(self.bandwidth or 0) // 1000}k"
        return f"v{self.index}@{(self.bandwidth or 0) // 1000}k"


@dataclass(slots=True)
class Rendition:
    """One `EXT-X-MEDIA` entry."""

    attrs: dict[str, str]
    line: Line
    resolved_uri: str | None

    @property
    def type(self) -> str:
        return self.attrs.get("TYPE", "")

    @property
    def group_id(self) -> str:
        return self.attrs.get("GROUP-ID", "")

    @property
    def name(self) -> str:
        return self.attrs.get("NAME", "")

    @property
    def language(self) -> str | None:
        return self.attrs.get("LANGUAGE")

    @property
    def channels(self) -> str | None:
        return self.attrs.get("CHANNELS")

    @property
    def default(self) -> bool:
        return self.attrs.get("DEFAULT", "NO").upper() == "YES"

    @property
    def variant_id(self) -> str:
        kind = {"AUDIO": "audio", "SUBTITLES": "sub", "CLOSED-CAPTIONS": "cc"}.get(
            self.type, self.type.lower() or "media"
        )
        return f"{kind}_{self.language or self.name or self.group_id}".replace(" ", "_")


@dataclass(slots=True)
class IFrameVariant:
    attrs: dict[str, str]
    line: Line
    resolved_uri: str


@dataclass(slots=True)
class MasterPlaylist:
    raw: str
    final_url: str
    variants: list[Variant] = field(default_factory=list)
    renditions: list[Rendition] = field(default_factory=list)
    iframe_variants: list[IFrameVariant] = field(default_factory=list)
    version: int | None = None
    independent_segments: bool = False
    session_keys: list[dict[str, str]] = field(default_factory=list)
    session_data: list[dict[str, str]] = field(default_factory=list)
    first_line: str = ""
    unknown_tags: list[Line] = field(default_factory=list)

    def audio_renditions(self, group_id: str | None = None) -> list[Rendition]:
        out = [r for r in self.renditions if r.type == "AUDIO"]
        return [r for r in out if r.group_id == group_id] if group_id else out

    def subtitle_renditions(self) -> list[Rendition]:
        return [r for r in self.renditions if r.type == "SUBTITLES"]

    @property
    def is_encrypted(self) -> bool:
        return bool(self.session_keys)


@dataclass(slots=True)
class Segment:
    """One media segment entry with everything a rule needs to judge it."""

    uri: str
    resolved_uri: str
    duration: float
    line: Line
    msn: int
    discontinuity_sequence: int
    discontinuity_before: bool = False
    program_date_time: str | None = None
    byterange: str | None = None
    key: dict[str, str] | None = None
    gap: bool = False
    map_uri: str | None = None
    title: str = ""
    cue_out: float | None = None
    cue_in: bool = False
    daterange: dict[str, str] | None = None


@dataclass(slots=True)
class MediaPlaylist:
    raw: str
    final_url: str
    target_duration: float | None = None
    media_sequence: int = 0
    discontinuity_sequence: int = 0
    version: int | None = None
    playlist_type: str | None = None
    endlist: bool = False
    iframes_only: bool = False
    independent_segments: bool = False
    segments: list[Segment] = field(default_factory=list)
    map_uri: str | None = None
    keys: list[dict[str, str]] = field(default_factory=list)
    dateranges: list[dict[str, str]] = field(default_factory=list)
    server_control: dict[str, str] = field(default_factory=dict)
    part_inf: dict[str, str] = field(default_factory=dict)
    first_line: str = ""
    unknown_tags: list[Line] = field(default_factory=list)
    looks_like_master: bool = False

    @property
    def is_live(self) -> bool:
        return not self.endlist and self.playlist_type != "VOD"

    @property
    def last_msn(self) -> int:
        return self.media_sequence + max(0, len(self.segments) - 1)

    @property
    def total_duration(self) -> float:
        return sum(s.duration for s in self.segments)

    @property
    def max_extinf(self) -> float:
        return max((s.duration for s in self.segments), default=0.0)

    @property
    def segment_uris(self) -> list[str]:
        return [s.resolved_uri for s in self.segments]

    @property
    def is_encrypted(self) -> bool:
        return any(k.get("METHOD", "NONE").upper() != "NONE" for k in self.keys)

    def segment_by_msn(self, msn: int) -> Segment | None:
        index = msn - self.media_sequence
        if 0 <= index < len(self.segments):
            return self.segments[index]
        return None


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def parse_master(text: str, final_url: str) -> MasterPlaylist:
    playlist = MasterPlaylist(raw=text, final_url=final_url)
    lines = text.splitlines()
    playlist.first_line = lines[0].strip() if lines else ""
    pending_stream_inf: tuple[dict[str, str], Line] | None = None
    variant_index = 0

    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        record = Line(number=number, text=raw_line)
        if not line.startswith("#"):
            if pending_stream_inf is not None:
                attrs, tag_line = pending_stream_inf
                playlist.variants.append(
                    Variant(
                        uri=line,
                        resolved_uri=resolve(final_url, line),
                        attrs=attrs,
                        line=tag_line,
                        index=variant_index,
                    )
                )
                variant_index += 1
                pending_stream_inf = None
            continue

        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_stream_inf = (parse_attributes(line.split(":", 1)[1]), record)
        elif line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attributes(line.split(":", 1)[1])
            uri = attrs.get("URI")
            playlist.renditions.append(
                Rendition(
                    attrs=attrs,
                    line=record,
                    resolved_uri=resolve(final_url, uri) if uri else None,
                )
            )
        elif line.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
            attrs = parse_attributes(line.split(":", 1)[1])
            playlist.iframe_variants.append(
                IFrameVariant(
                    attrs=attrs,
                    line=record,
                    resolved_uri=resolve(final_url, attrs.get("URI", "")),
                )
            )
        elif line.startswith("#EXT-X-VERSION:"):
            playlist.version = parse_int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-INDEPENDENT-SEGMENTS"):
            playlist.independent_segments = True
        elif line.startswith("#EXT-X-SESSION-KEY:"):
            playlist.session_keys.append(parse_attributes(line.split(":", 1)[1]))
        elif line.startswith("#EXT-X-SESSION-DATA:"):
            playlist.session_data.append(parse_attributes(line.split(":", 1)[1]))
        elif line.startswith("#EXT-X-") and ":" in line:
            playlist.unknown_tags.append(record)

    return playlist


def parse_media(text: str, final_url: str) -> MediaPlaylist:
    playlist = MediaPlaylist(raw=text, final_url=final_url)
    lines = text.splitlines()
    playlist.first_line = lines[0].strip() if lines else ""
    playlist.looks_like_master = is_master(text)

    pending_duration: float | None = None
    pending_title = ""
    pending_line: Line | None = None
    pending_discontinuity = False
    pending_pdt: str | None = None
    pending_byterange: str | None = None
    pending_gap = False
    pending_cue_out: float | None = None
    pending_cue_in = False
    pending_daterange: dict[str, str] | None = None
    current_key: dict[str, str] | None = None
    current_map: str | None = None
    msn = 0
    dsn = 0
    seen_first_segment = False

    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        record = Line(number=number, text=raw_line)

        if not line.startswith("#"):
            if pending_duration is None:
                continue
            if not seen_first_segment:
                msn = playlist.media_sequence
                dsn = playlist.discontinuity_sequence
                seen_first_segment = True
                if pending_discontinuity:
                    dsn += 1
            elif pending_discontinuity:
                dsn += 1
            playlist.segments.append(
                Segment(
                    uri=line,
                    resolved_uri=resolve(final_url, line),
                    duration=pending_duration,
                    line=pending_line or record,
                    msn=msn,
                    discontinuity_sequence=dsn,
                    discontinuity_before=pending_discontinuity,
                    program_date_time=pending_pdt,
                    byterange=pending_byterange,
                    key=dict(current_key) if current_key else None,
                    gap=pending_gap,
                    map_uri=current_map,
                    title=pending_title,
                    cue_out=pending_cue_out,
                    cue_in=pending_cue_in,
                    daterange=pending_daterange,
                )
            )
            msn += 1
            pending_duration = None
            pending_title = ""
            pending_line = None
            pending_discontinuity = False
            pending_pdt = None
            pending_byterange = None
            pending_gap = False
            pending_cue_out = None
            pending_cue_in = False
            pending_daterange = None
            continue

        if line.startswith("#EXTINF:"):
            body = line.split(":", 1)[1]
            duration_text, _, title = body.partition(",")
            parsed = _f(duration_text.strip())
            pending_duration = parsed if parsed is not None else -1.0
            pending_title = title.strip()
            pending_line = record
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            playlist.target_duration = _f(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            playlist.media_sequence = parse_int(line.split(":", 1)[1]) or 0
        elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            playlist.discontinuity_sequence = parse_int(line.split(":", 1)[1]) or 0
        elif line == "#EXT-X-DISCONTINUITY":
            pending_discontinuity = True
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_pdt = line.split(":", 1)[1].strip()
        elif line.startswith("#EXT-X-BYTERANGE:"):
            pending_byterange = line.split(":", 1)[1].strip()
        elif line == "#EXT-X-GAP":
            pending_gap = True
        elif line.startswith("#EXT-X-KEY:"):
            current_key = parse_attributes(line.split(":", 1)[1])
            playlist.keys.append(current_key)
        elif line.startswith("#EXT-X-MAP:"):
            attrs = parse_attributes(line.split(":", 1)[1])
            uri = attrs.get("URI")
            current_map = resolve(final_url, uri) if uri else None
            playlist.map_uri = playlist.map_uri or current_map
        elif line.startswith("#EXT-X-VERSION:"):
            playlist.version = parse_int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-PLAYLIST-TYPE:"):
            playlist.playlist_type = line.split(":", 1)[1].strip().upper()
        elif line == "#EXT-X-ENDLIST":
            playlist.endlist = True
        elif line == "#EXT-X-I-FRAMES-ONLY":
            playlist.iframes_only = True
        elif line == "#EXT-X-INDEPENDENT-SEGMENTS":
            playlist.independent_segments = True
        elif line.startswith("#EXT-X-CUE-OUT"):
            body = line.split(":", 1)[1] if ":" in line else ""
            attrs = parse_attributes(body)
            pending_cue_out = _f(attrs.get("DURATION") or body.strip()) or 0.0
        elif line.startswith("#EXT-X-CUE-IN"):
            pending_cue_in = True
        elif line.startswith("#EXT-X-DATERANGE:"):
            attrs = parse_attributes(line.split(":", 1)[1])
            attrs["_line"] = str(number)
            playlist.dateranges.append(attrs)
            pending_daterange = attrs
        elif line.startswith("#EXT-X-SERVER-CONTROL:"):
            playlist.server_control = parse_attributes(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-PART-INF:"):
            playlist.part_inf = parse_attributes(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-") and not line.startswith("#EXT-X-STREAM-INF"):
            playlist.unknown_tags.append(record)

    return playlist


def parse(text: str, final_url: str) -> MasterPlaylist | MediaPlaylist:
    return parse_master(text, final_url) if is_master(text) else parse_media(text, final_url)


def summarise(playlist: MediaPlaylist) -> dict[str, Any]:
    return {
        "target_duration": playlist.target_duration,
        "media_sequence": playlist.media_sequence,
        "discontinuity_sequence": playlist.discontinuity_sequence,
        "segment_count": len(playlist.segments),
        "window_s": playlist.total_duration,
        "endlist": playlist.endlist,
        "last_msn": playlist.last_msn,
    }

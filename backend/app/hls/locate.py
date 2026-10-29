"""Which lines of a playlist a finding is about.

A finding carries the measurements it fired on — a media sequence number, a segment URI, a
line number the parser recorded, a tag its sentence names — and the playlist it was measured
on is stored in the poll log. This module turns the first into line numbers of the second, so
the manifest shown beside a finding marks the segment or tag at fault rather than leaving an
engineer to scroll a five-hundred-line window for it.

Every highlight states why it was chosen. A finding with nothing that names a line (a stall,
a ladder-wide measurement) gets none, and the caller says so rather than marking a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

# Evidence keys that hold one media sequence number, or several.
MSN_KEYS = (
    "msn",
    "previous_msn",
    "current_msn",
    "msn_a",
    "msn_b",
    "start_msn",
    "anchor_msn",
    "video_msn",
    "cdn_msn",
    "origin_msn",
)
MSN_LIST_KEYS = ("msns",)
URI_KEYS = ("uri", "previous_uri", "current_uri", "segment_uri", "url", "final_url")
LINE_KEYS = ("line", "duplicate_of_line")

# A tag named in a finding's title or sentence, e.g. EXT-X-DISCONTINUITY or #EXTINF.
_TAG = re.compile(r"#?(EXT-X-[A-Z0-9-]+|EXTINF|EXT-OATCLS-SCTE35)")

MAX_HIGHLIGHTS = 40


@dataclass(slots=True)
class Highlight:
    line: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "reason": self.reason}


@dataclass(slots=True)
class _Segment:
    msn: int
    extinf_line: int
    uri_line: int
    uri: str


def _segments(lines: list[str]) -> list[_Segment]:
    media_sequence = 0
    for text in lines:
        if text.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(text.split(":", 1)[1].strip())
            except ValueError:
                media_sequence = 0
            break
    found: list[_Segment] = []
    pending_extinf = 0
    index = 0
    for number, text in enumerate(lines, start=1):
        stripped = text.strip()
        # The tag a URI line belongs to: #EXTINF in a media playlist, #EXT-X-STREAM-INF in a
        # master, so a rendition named by a master finding marks both of its lines.
        if stripped.startswith(("#EXTINF", "#EXT-X-STREAM-INF")):
            pending_extinf = number
        elif stripped and not stripped.startswith("#"):
            found.append(
                _Segment(
                    msn=media_sequence + index,
                    extinf_line=pending_extinf or number,
                    uri_line=number,
                    uri=stripped,
                )
            )
            index += 1
            pending_extinf = 0
    return found


def _path_tail(uri: str) -> str:
    path = urlsplit(uri).path
    return path.rsplit("/", 1)[-1]


def _same_uri(line_uri: str, wanted: str, base_url: str) -> bool:
    if not line_uri or not wanted:
        return False
    if line_uri == wanted:
        return True
    if base_url:
        resolved = urljoin(base_url, line_uri)
        # Tokens differ per request; the path the line names does not.
        return urlsplit(resolved).path == urlsplit(wanted).path and bool(urlsplit(wanted).path)
    # With nothing to resolve against, the file name is all there is to compare.
    return bool(_path_tail(line_uri)) and _path_tail(line_uri) == _path_tail(wanted)


def _ints(value: Any) -> list[int]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, int) and not isinstance(v, bool)]
    return []


def locate(
    raw: str,
    *,
    evidence: dict[str, Any] | None = None,
    text: str = "",
    base_url: str = "",
) -> list[Highlight]:
    """Lines of `raw` the evidence and the finding's own words point at, in line order."""
    lines = raw.splitlines()
    if not lines:
        return []
    evidence = evidence or {}
    marks: dict[int, str] = {}

    def mark(number: int, reason: str) -> None:
        if 1 <= number <= len(lines) and number not in marks:
            marks[number] = reason

    for key in LINE_KEYS:
        for number in _ints(evidence.get(key)):
            mark(number, f"line {number} named by the finding ({key})")

    segments = _segments(lines)
    wanted_msns: list[tuple[int, str]] = []
    for key in MSN_KEYS:
        wanted_msns += [(msn, key) for msn in _ints(evidence.get(key))]
    for key in MSN_LIST_KEYS:
        wanted_msns += [(msn, key) for msn in _ints(evidence.get(key))]
    by_msn = {segment.msn: segment for segment in segments}
    for msn, key in wanted_msns:
        segment = by_msn.get(msn)
        if segment is not None:
            mark(segment.extinf_line, f"segment MSN {msn} ({key})")
            mark(segment.uri_line, f"segment MSN {msn} ({key})")

    for key in URI_KEYS:
        value = evidence.get(key)
        if not isinstance(value, str):
            continue
        for segment in segments:
            if _same_uri(segment.uri, value, base_url):
                mark(segment.extinf_line, f"the segment the finding names ({key})")
                mark(segment.uri_line, f"the segment the finding names ({key})")

    tags = {match.group(1) for match in _TAG.finditer(text)}
    for tag in sorted(tags):
        prefix = "#" + tag
        hits = [n for n, line in enumerate(lines, start=1) if line.startswith(prefix)]
        # A tag on every segment (EXTINF) marks nothing useful by itself.
        if tag == "EXTINF" and len(hits) > 3:
            continue
        for number in hits[:MAX_HIGHLIGHTS]:
            mark(number, f"{prefix} named by the finding")

    return [Highlight(line=n, reason=marks[n]) for n in sorted(marks)][:MAX_HIGHLIGHTS]

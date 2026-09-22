"""The segment bytes an evidence bundle is built from.

`build_evidence_bundle` wrote `result.json` and the playlist snapshots and claimed in its own
docstring to write "segments and playlist snapshots". It wrote no segment bytes at all —
neither encrypted nor clear — so nobody downloading an evidence bundle for a stream defect
ever got the stream. That affected clear channels as much as protected ones.

Segments are held here, in memory, for the life of the run, and streamed into the archive
when one is asked for. Nothing is written to the analyzer host: the host keeps no state, and
a bundle is built and streamed the same way a report is.

**What is kept, and why not everything.** A rung sampled in full for twenty-four hours is
tens of gigabytes; a bundle cannot be that and neither can this process. The store holds a
rolling window of the most recent segments per rendition under one overall byte budget, and
a segment sampled while an incident was open is **pinned**: pinned segments are evicted only
after every unpinned one is gone, so the bytes around a defect survive the hours of clean
stream that follow it. Both bounds are thresholds, so a deployment with memory to spare can
keep more.

**A protected segment is kept twice** — as it was served, and as it was decrypted — because
the two answer different questions. The encrypted bytes prove what the CDN sent; the
decrypted ones are what a decoder reads, and are what a packager needs to reproduce a
bitstream defect. Neither carries the key: the key is in `KeyStore`, in memory, for the life
of the job, and nothing here has a reference to it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

# A clear segment's own bytes are what a decoder reads, so it is kept once.
CLEAR = "clear"
ENCRYPTED = "encrypted"


@dataclass(slots=True)
class EvidenceSegment:
    """One sampled segment's bytes, with what a reader needs to place it."""

    variant: str
    kind: str
    msn: int
    uri: str
    at: dt.datetime
    # As served. For a clear segment this is the whole of it.
    raw: bytes
    # The decrypted fragment with its initialisation segment in front, so what is written is a
    # file a decoder opens rather than a fragment it cannot. Empty for a clear segment, and
    # for a protected one whose key was never obtained.
    decrypted: bytes = b""
    # Why a protected segment has no decrypted copy. Empty when it has one, or when it was
    # never encrypted.
    drm_reason: str = ""
    # Sampled while an incident was open, so the last thing evicted.
    pinned: bool = False

    @property
    def encrypted(self) -> bool:
        return bool(self.drm_reason) or bool(self.decrypted)

    @property
    def size(self) -> int:
        return len(self.raw) + len(self.decrypted)

    @property
    def extension(self) -> str:
        """The container's own extension, taken from the URI and defaulted per container."""
        tail = self.uri.split("?")[0].rsplit("/", 1)[-1]
        if "." in tail:
            suffix = tail.rsplit(".", 1)[-1].lower()
            if suffix.isalnum() and len(suffix) <= 4:
                return suffix
        return "m4s"

    def manifest_row(self, path: str) -> dict[str, object]:
        return {
            "path": path,
            "variant": self.variant,
            "kind": self.kind,
            "msn": self.msn,
            "source_uri": self.uri,
            "sampled_at": self.at.isoformat(),
            "bytes": len(self.decrypted or self.raw),
        }


@dataclass
class EvidenceStore:
    """Every segment held for the bundle, under one byte budget."""

    max_bytes: int = 64 * 1024 * 1024
    per_rendition: int = 20

    segments: list[EvidenceSegment] = field(default_factory=list)
    # Counted rather than derived, so a long run does not re-walk the list on every sample.
    held_bytes: int = 0
    # How many were dropped to stay inside the budget. Stated in the bundle's README, because
    # a bundle that silently holds a fraction of what was sampled is a bundle nobody can
    # reason about.
    evicted: int = 0

    def add(self, segment: EvidenceSegment) -> None:
        if not segment.raw and not segment.decrypted:
            return
        self.segments.append(segment)
        self.held_bytes += segment.size
        self._evict()

    def _evict(self) -> None:
        """Oldest unpinned first, then oldest pinned, until both bounds are met."""
        self._trim_per_rendition()
        while self.segments and self.held_bytes > self.max_bytes:
            index = next(
                (i for i, s in enumerate(self.segments) if not s.pinned),
                0,  # everything left is pinned; the oldest of those goes.
            )
            self._drop(index)

    def _trim_per_rendition(self) -> None:
        counts: dict[str, int] = {}
        for segment in self.segments:
            counts[segment.variant] = counts.get(segment.variant, 0) + 1
        for variant, count in counts.items():
            excess = count - self.per_rendition
            while excess > 0:
                index = next(
                    (
                        i
                        for i, s in enumerate(self.segments)
                        if s.variant == variant and not s.pinned
                    ),
                    next(i for i, s in enumerate(self.segments) if s.variant == variant),
                )
                self._drop(index)
                excess -= 1

    def _drop(self, index: int) -> None:
        segment = self.segments.pop(index)
        self.held_bytes -= segment.size
        self.evicted += 1

    # -- what the bundle asks -------------------------------------------------

    @property
    def count(self) -> int:
        return len(self.segments)

    @property
    def decrypted_count(self) -> int:
        return sum(1 for s in self.segments if s.decrypted)

    @property
    def encrypted_count(self) -> int:
        return sum(1 for s in self.segments if s.encrypted)

    def reasons(self) -> dict[str, int]:
        """Why protected segments have no decrypted copy, and how many for each reason."""
        out: dict[str, int] = {}
        for segment in self.segments:
            if segment.drm_reason and not segment.decrypted:
                out[segment.drm_reason] = out.get(segment.drm_reason, 0) + 1
        return out

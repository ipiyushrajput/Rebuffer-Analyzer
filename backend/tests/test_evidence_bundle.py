"""What an evidence bundle actually contains.

`build_evidence_bundle` wrote `result.json` and the playlist snapshots, and its own docstring
said "segments and playlist snapshots". It wrote no segment bytes at all — for a clear
channel as much as a protected one — so nobody downloading evidence for a stream defect ever
got the stream.

The media is held in memory for the life of the run and streamed into the archive. A run is
hours long and a rung sampled in full is tens of gigabytes, so the store is bounded; what it
drops, and why, is stated in the bundle's own README rather than left for a reader to notice.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import zipfile

import pytest

from app.analysis import evidence as ev
from app.analysis.engine import SessionOptions
from app.config import Thresholds
from app.drm import decrypt as drm_decrypt
from app.drm.settings import DrmSettings

NOW = dt.datetime(2026, 9, 22, 10, 0, 0, tzinfo=dt.UTC)


def _segment(
    msn: int,
    *,
    variant: str = "v720p@2000k",
    kind: str = "video",
    size: int = 1000,
    decrypted: bool = False,
    reason: str = "",
    pinned: bool = False,
    uri: str | None = None,
) -> ev.EvidenceSegment:
    return ev.EvidenceSegment(
        variant=variant,
        kind=kind,
        msn=msn,
        uri=uri or f"https://cdn.example/{variant}/{msn}.m4s",
        at=NOW,
        raw=b"\x00" * size,
        decrypted=b"\x01" * size if decrypted else b"",
        drm_reason=reason,
        pinned=pinned,
    )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def test_a_segment_is_held_with_both_copies_when_it_was_protected() -> None:
    store = ev.EvidenceStore()

    store.add(_segment(1, decrypted=True))

    held = store.segments[0]
    assert held.encrypted
    assert held.size == 2000, "as served and as decrypted are both kept"
    assert store.decrypted_count == 1


def test_a_clear_segment_is_kept_once() -> None:
    store = ev.EvidenceStore()

    store.add(_segment(1))

    assert not store.segments[0].encrypted
    assert store.encrypted_count == 0


def test_a_segment_with_no_bytes_is_not_held() -> None:
    store = ev.EvidenceStore()

    store.add(_segment(1, size=0))

    assert store.count == 0


def test_the_store_stays_inside_its_byte_budget() -> None:
    store = ev.EvidenceStore(max_bytes=3000, per_rendition=100)

    for msn in range(10):
        store.add(_segment(msn, size=1000))

    assert store.held_bytes <= 3000
    assert store.evicted == 7
    assert [s.msn for s in store.segments] == [7, 8, 9], "the newest survive"


def test_a_segment_sampled_during_an_incident_is_evicted_last() -> None:
    """The bytes around a defect are the point of the bundle; the clean hours after it are
    not. A pinned segment goes only once every unpinned one is already gone."""
    store = ev.EvidenceStore(max_bytes=2000, per_rendition=100)

    store.add(_segment(1, size=1000, pinned=True))
    for msn in range(2, 6):
        store.add(_segment(msn, size=1000))

    assert 1 in {s.msn for s in store.segments}


def test_each_rendition_keeps_its_own_window() -> None:
    """One noisy rung must not evict every other rung's evidence."""
    store = ev.EvidenceStore(max_bytes=10**9, per_rendition=2)

    for msn in range(5):
        store.add(_segment(msn, variant="v720p@2000k"))
    store.add(_segment(0, variant="audio_en", kind="audio"))

    by_variant: dict[str, int] = {}
    for segment in store.segments:
        by_variant[segment.variant] = by_variant.get(segment.variant, 0) + 1
    assert by_variant == {"v720p@2000k": 2, "audio_en": 1}


def test_the_reasons_a_segment_was_not_decrypted_are_counted() -> None:
    store = ev.EvidenceStore()
    store.add(_segment(1, reason="The key server holds no key for that identifier."))
    store.add(_segment(2, reason="The key server holds no key for that identifier."))
    store.add(_segment(3, decrypted=True))

    assert store.reasons() == {"The key server holds no key for that identifier.": 2}


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("https://cdn.example/a/0.ts", "ts"),
        ("https://cdn.example/a/0.m4s?token=abc", "m4s"),
        ("https://cdn.example/a/0.mp4", "mp4"),
        ("https://cdn.example/a/segment", "m4s"),
    ],
)
def test_a_segment_keeps_its_own_container_extension(uri: str, expected: str) -> None:
    assert _segment(1, uri=uri).extension == expected


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, store: ev.EvidenceStore | None) -> None:
        self.evidence = store
        self.layers: dict[str, object] = {}


class _Handle:
    def __init__(self, store: ev.EvidenceStore | None) -> None:
        self.id = "job-1"
        self.channel_name = "Fixture HD"
        self.result = None
        self.options = SessionOptions(ua_profile="tizen10", record_evidence=True)
        self.session = _Session(store)


def _archive(store: ev.EvidenceStore | None, *, decrypt_evidence: bool) -> zipfile.ZipFile:
    from app.reports.service import _write_segments

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        _write_segments(bundle, _Handle(store), decrypt_evidence=decrypt_evidence)  # type: ignore[arg-type]
    return zipfile.ZipFile(io.BytesIO(buffer.getvalue()))


def test_a_clear_segment_lands_under_segments() -> None:
    store = ev.EvidenceStore()
    store.add(_segment(41))

    names = _archive(store, decrypt_evidence=False).namelist()

    assert names == ["segments/v720p-2000k/000000000041.m4s"]


def test_a_protected_segment_lands_under_encrypted_as_it_was_served() -> None:
    store = ev.EvidenceStore()
    store.add(_segment(41, decrypted=True))

    archive = _archive(store, decrypt_evidence=False)

    assert archive.namelist() == ["encrypted/v720p-2000k/000000000041.m4s"]
    assert archive.read("encrypted/v720p-2000k/000000000041.m4s") == b"\x00" * 1000


def test_the_decrypted_copy_is_written_only_when_the_setting_allows_it() -> None:
    store = ev.EvidenceStore()
    store.add(_segment(41, decrypted=True))

    names = _archive(store, decrypt_evidence=True).namelist()

    assert "encrypted/v720p-2000k/000000000041.m4s" in names
    assert "decrypted/video/v720p-2000k/000000000041.mp4" in names


def test_the_decrypted_manifest_maps_every_file_back_to_its_source() -> None:
    store = ev.EvidenceStore()
    store.add(_segment(41, decrypted=True))
    store.add(_segment(7, variant="audio_en", kind="audio", decrypted=True))

    archive = _archive(store, decrypt_evidence=True)
    manifest = json.loads(archive.read("decrypted/manifest.json"))

    assert {row["path"] for row in manifest} == {
        "decrypted/video/v720p-2000k/000000000041.mp4",
        "decrypted/audio/audio_en/000000000007.mp4",
    }
    video = next(r for r in manifest if r["variant"] == "v720p@2000k")
    assert video["msn"] == 41
    assert video["source_uri"] == "https://cdn.example/v720p@2000k/41.m4s"


def test_a_run_that_recorded_nothing_writes_no_segment_folder() -> None:
    assert _archive(None, decrypt_evidence=True).namelist() == []


def test_files_sort_in_media_sequence_order() -> None:
    """Zero-padded, so a directory listing and a sorted extract agree with the timeline."""
    store = ev.EvidenceStore()
    for msn in (9, 10, 100):
        store.add(_segment(msn))

    names = sorted(_archive(store, decrypt_evidence=False).namelist())

    assert [n.rsplit("/", 1)[-1] for n in names] == [
        "000000000009.m4s",
        "000000000010.m4s",
        "000000000100.m4s",
    ]


# ---------------------------------------------------------------------------
# The README, which must never leave a reader guessing
# ---------------------------------------------------------------------------


def _readme(store: ev.EvidenceStore | None, *, decrypt_evidence: bool) -> str:
    from app.reports.service import _bundle_readme

    return _bundle_readme(_Handle(store), decrypt_evidence=decrypt_evidence)  # type: ignore[arg-type]


def test_the_readme_says_when_the_run_recorded_no_evidence() -> None:
    text = _readme(None, decrypt_evidence=True)

    assert "was not asked to record evidence" in text


def test_the_readme_says_when_decryption_is_switched_off() -> None:
    store = ev.EvidenceStore()
    store.add(_segment(1, decrypted=True))

    text = _readme(store, decrypt_evidence=False)

    assert "switched off in Settings" in text


def test_the_readme_states_the_reason_a_key_was_not_obtained() -> None:
    store = ev.EvidenceStore()
    store.add(_segment(1, reason="The key server holds no key for that identifier."))

    text = _readme(store, decrypt_evidence=True)

    assert "The key server holds no key for that identifier." in text


def test_the_readme_states_the_cbcs_reason_verbatim() -> None:
    reason = (
        "This track uses the cbcs scheme, which needs Bento4's mp4decrypt, and that binary "
        "is not installed on this analyzer host."
    )
    store = ev.EvidenceStore()
    store.add(_segment(1, reason=reason))

    assert reason in _readme(store, decrypt_evidence=True)


def test_the_readme_says_how_many_segments_were_dropped_to_stay_in_budget() -> None:
    store = ev.EvidenceStore(max_bytes=2000, per_rendition=100)
    for msn in range(6):
        store.add(_segment(msn, size=1000))

    text = _readme(store, decrypt_evidence=True)

    assert "4 older segment(s) were dropped" in text
    assert "evidence_max_bytes" in text


def test_the_readme_counts_what_it_holds_and_names_no_key() -> None:
    store = ev.EvidenceStore()
    store.add(_segment(1, decrypted=True))
    store.add(_segment(2))

    text = _readme(store, decrypt_evidence=True)

    assert "Segments held: 2." in text
    assert "No content key, private key or CPIX credential is in this bundle." in text


# ---------------------------------------------------------------------------
# The decrypt backends
# ---------------------------------------------------------------------------


def test_both_backends_are_reported_so_a_host_knows_what_it_can_read() -> None:
    reported = drm_decrypt.backends()

    assert set(reported) == {drm_decrypt.IN_PROCESS, drm_decrypt.MP4DECRYPT}
    assert reported[drm_decrypt.IN_PROCESS] is True


def test_mp4decrypt_refuses_rather_than_guessing_when_the_binary_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drm_decrypt, "mp4decrypt_path", lambda: None)

    result = drm_decrypt.decrypt_with_mp4decrypt(b"x" * 100, key_hex="00" * 16, kid_hex="11" * 16)

    assert not result.ok
    assert "mp4decrypt" in result.reason
    assert "not installed" in result.reason


def test_mp4decrypt_without_a_key_is_refused_before_a_process_is_started() -> None:
    result = drm_decrypt.decrypt_with_mp4decrypt(b"x" * 100, key_hex="", kid_hex="")

    assert not result.ok
    assert result.backend == ""


def test_a_result_names_the_backend_that_produced_it() -> None:
    """A reader of an evidence bundle has to be able to tell which cipher path ran."""
    assert drm_decrypt.IN_PROCESS == "cenc"
    assert drm_decrypt.MP4DECRYPT == "mp4decrypt"


def test_decrypting_evidence_is_off_until_a_deployment_turns_it_on() -> None:
    """Decrypted media is the content in the clear, in a file that gets forwarded."""
    assert DrmSettings().decrypt_evidence is False
    assert DrmSettings().describe()["decrypt_evidence"] is False


def test_the_evidence_budget_is_a_threshold_not_a_literal() -> None:
    thresholds = Thresholds()

    assert thresholds.evidence_max_bytes > 0
    assert thresholds.evidence_segments_per_rendition > 0

"""A run can be recorded: every segment of every rendition, on disk, downloadable and playable.

The evidence store holds a dozen sampled segments in memory and loses them with the session;
the sampler fetches only what the rules need. Recording is the third thing: every segment each
playlist lists, written to the recordings folder as it is listed — in full for realtime, as
incident clips for aging — decrypted when the run holds the key, and downloaded as one archive
with each rendition joined into a file a player opens and the network log of the same minutes.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.analysis import recorder as recorder_module
from app.analysis.recorder import INCIDENTS, RecordedSegment, StreamRecorder, read_summary
from app.db import session as db_session
from app.db.models import ManifestBody, ManifestPoll
from app.hls.playlist import parse_media
from app.main import create_app
from app.net.fetcher import Fetcher
from app.reports.recording import clips_of, recording_state, stream_recording
from tests.fixtures.server import FixtureServer, Route, build_simple_channel
from tests.test_drm_analysis import PLAIN, encrypted_fragment, widevine_context
from tests.test_drm_cenc import KEY, KID, init_segment


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


async def _archive(root: Path, job_id: str) -> zipfile.ZipFile:
    data = b"".join([chunk async for chunk in stream_recording(root, job_id)])
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert archive.testzip() is None
    return archive


# -- the recorder on its own ----------------------------------------------------


@pytest.mark.asyncio()
async def test_a_protected_rendition_is_recorded_as_served_and_as_decrypted(
    origin: FixtureServer, tmp_path: Path
) -> None:
    origin.add("/init.mp4", Route(body=init_segment(), content_type="video/mp4"))
    origin.add("/seg1.m4s", Route(body=encrypted_fragment(), content_type="video/mp4"))
    origin.add("/seg2.m4s", Route(body=encrypted_fragment(), content_type="video/mp4"))
    context = widevine_context({KID: KEY.hex()})
    await context.read_init("video_720", init_segment())

    playlist = parse_media(
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:1\n"
        '#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6.0,\nseg1.m4s\n#EXTINF:6.0,\nseg2.m4s\n',
        origin.url("/video.m3u8"),
    )
    fetcher = Fetcher()
    recorder = StreamRecorder(job_id="drm-rec", root=tmp_path, fetcher=fetcher, drm=lambda: context)
    try:
        recorder.offer("video_720", 1, encrypted_fragment())  # the sampler already had MSN 1
        recorder.observe_playlist(
            layer="PLAYBACK", variant="video_720", kind="video", playlist=playlist, encrypted=True
        )
        recorder.observe_playlist(  # a repeat poll lists them again; nothing is queued twice
            layer="PLAYBACK", variant="video_720", kind="video", playlist=playlist, encrypted=True
        )
        summary = await recorder.close()
    finally:
        await fetcher.aclose()

    segments = summary["segments"]
    assert [s["msn"] for s in segments] == [1, 2]
    assert all(s["decrypted_file"] for s in segments)
    directory = tmp_path / "drm-rec"
    assert PLAIN in (directory / segments[0]["decrypted_file"]).read_bytes()
    assert PLAIN not in (directory / segments[0]["file"]).read_bytes(), "as served stays encrypted"
    assert (directory / segments[0]["init"]).read_bytes() == init_segment()
    assert KEY.hex() not in json.dumps(summary), "no key material in the summary"

    archive = await _archive(tmp_path, "drm-rec")
    names = archive.namelist()
    joined = archive.read("full/PLAYBACK_video_720.mp4")
    assert joined.startswith(init_segment()), "the init segment leads, so a player opens it"
    decrypted = archive.read("full/decrypted/PLAYBACK_video_720.mp4")
    assert decrypted.count(PLAIN) == 2
    assert "README.txt" in names and "recording.json" in names


@pytest.mark.asyncio()
async def test_incident_mode_keeps_only_what_an_incident_spans(tmp_path: Path) -> None:
    fetcher = Fetcher()
    recorder = StreamRecorder(
        job_id="clips", root=tmp_path, fetcher=fetcher, mode=INCIDENTS, preroll_s=30, postroll_s=10
    )
    base = dt.datetime(2026, 10, 13, 9, 0, tzinfo=dt.UTC)
    directory = tmp_path / "clips"
    for minute in range(10):
        relative = f"PLAYBACK/v1/{minute:010d}.ts"
        (directory / relative).parent.mkdir(parents=True, exist_ok=True)
        (directory / relative).write_bytes(b"x" * 10)
        recorder.entries.append(
            RecordedSegment(
                layer="PLAYBACK",
                variant="v1",
                kind="video",
                msn=minute,
                uri=f"s{minute}.ts",
                at=(base + dt.timedelta(minutes=minute)).isoformat(),
                duration=6.0,
                file=relative,
                bytes=10,
                status=200,
            )
        )
    # One incident from 09:05:00 to 09:05:20: kept from 09:04:30 to 09:05:30.
    recorder.set_windows(
        [("v1", base + dt.timedelta(minutes=5), base + dt.timedelta(minutes=5, seconds=20))]
    )
    removed = await recorder.prune(force=True, now=base + dt.timedelta(hours=1))
    await fetcher.aclose()

    assert removed == 9
    assert [e.msn for e in recorder.entries] == [5]
    assert sorted(p.name for p in (directory / "PLAYBACK/v1").iterdir()) == ["0000000005.ts"]

    clips = clips_of(
        {
            "mode": INCIDENTS,
            "preroll_s": 30,
            "postroll_s": 10,
            "segments": [
                {"at": (base + dt.timedelta(minutes=5)).isoformat()},
                {"at": (base + dt.timedelta(minutes=9)).isoformat()},
            ],
            "windows": [
                {
                    "variant": "v1",
                    "start": (base + dt.timedelta(minutes=5)).isoformat(),
                    "end": None,
                },
                {
                    "variant": "v2",
                    "start": (base + dt.timedelta(minutes=5, seconds=5)).isoformat(),
                    "end": (base + dt.timedelta(minutes=5, seconds=6)).isoformat(),
                },
            ],
            "ended_at": (base + dt.timedelta(minutes=6)).isoformat(),
        }
    )
    assert len(clips) == 1, "overlapping incidents are one clip"
    assert "v1" in clips[0].label and "v2" in clips[0].label
    assert len(clips[0].segments) == 1


@pytest.mark.asyncio()
async def test_a_recorder_that_reaches_its_quota_stops_and_says_so(
    origin: FixtureServer, tmp_path: Path
) -> None:
    origin.add("/a.ts", Route(body=b"\x47" * 188 * 10, content_type="video/mp2t"))
    playlist = parse_media(
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:6.0,\na.ts\n",
        origin.url("/v.m3u8"),
    )
    recorder_module.USAGE.root = None
    fetcher = Fetcher()
    recorder = StreamRecorder(job_id="quota", root=tmp_path, fetcher=fetcher, quota_bytes=100)
    recorder.observe_playlist(
        layer="PLAYBACK", variant="v", kind="video", playlist=playlist, encrypted=False
    )
    summary = await recorder.close()
    await fetcher.aclose()

    assert "quota" in summary["stopped_reason"]
    assert summary["segments"] == []


def test_the_purge_keeps_running_jobs_and_clips_longer(tmp_path: Path) -> None:
    now = dt.datetime.now(dt.UTC)
    for name, mode in (("old-full", "full"), ("old-clips", INCIDENTS), ("running", "full")):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "recording.json").write_text(json.dumps({"mode": mode, "segments": []}))
        stamp = (now - dt.timedelta(days=60)).timestamp()
        os.utime(directory, (stamp, stamp))
    removed = recorder_module.purge(
        tmp_path,
        cutoff=now - dt.timedelta(days=30),
        pinned_cutoff=now - dt.timedelta(days=180),
        running={"running"},
    )
    assert removed == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["old-clips", "running"]


def test_a_run_with_no_recording_says_so(tmp_path: Path) -> None:
    assert recording_state(tmp_path, "nothing") == {"available": False}
    assert read_summary(tmp_path, "nothing") is None


# -- a real run --------------------------------------------------------------


@pytest.mark.asyncio()
async def test_a_recorded_run_holds_every_listed_segment_and_its_network_log(
    origin: FixtureServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.schemas import JobOptionsIn
    from app.config import get_settings
    from app.jobs.manager import job_manager

    monkeypatch.setattr(get_settings(), "rba_recordings_dir", tmp_path)
    url = build_simple_channel(origin, variant_count=2, segment_count=6)
    await job_manager.start()
    handle = await job_manager.submit(
        job_type="realtime",
        playback_url=url,
        channel_name="Recorded channel",
        options=JobOptionsIn(record_stream=True).to_session_options(
            duration_s=5.0, default_record=False
        ),
    )
    assert handle.task is not None
    await handle.task
    try:
        state = recording_state(tmp_path, handle.id)
        assert state["available"] and state["mode"] == "full"
        summary = read_summary(tmp_path, handle.id)
        assert summary is not None
        by_variant: dict[str, set[int]] = {}
        for segment in summary["segments"]:
            by_variant.setdefault(segment["variant"], set()).add(segment["msn"])
        assert len(by_variant) == 2
        assert all(msns == set(range(6)) for msns in by_variant.values()), by_variant

        archive = await _archive(tmp_path, handle.id)
        names = archive.namelist()
        media = [n for n in names if n.startswith("full/") and n.endswith(".ts")]
        assert len(media) == 2
        first = next(n for n in media if "360" in n or "low" in n.lower() or n)
        assert archive.read(first).startswith(b"\x47"), "a joined transport stream"
        assert any(n.startswith("full/network/polls/") for n in names)
        assert "full/network/network.har" in names
    finally:
        async with db_session.session_scope() as session:
            for model in (ManifestPoll, ManifestBody):
                await session.execute(delete(model).where(model.job_id == handle.id))


@pytest.mark.asyncio()
async def test_evidence_of_a_run_no_longer_in_memory_is_built_from_the_database(
    origin: FixtureServer,
) -> None:
    from app.api import reports as reports_api
    from app.api.schemas import JobOptionsIn
    from app.jobs.manager import job_manager

    url = build_simple_channel(origin, variant_count=1, segment_count=6)
    origin.remove("low-seg2.ts")
    await job_manager.start()
    handle = await job_manager.submit(
        job_type="aging",
        playback_url=url,
        channel_name="Restarted channel",
        options=JobOptionsIn().to_session_options(duration_s=4.0, default_record=True),
    )
    assert handle.task is not None
    await handle.task
    try:
        job_manager.jobs.pop(handle.id)  # as after a restart
        response = await reports_api.evidence_bundle(handle.id)
        archive = zipfile.ZipFile(io.BytesIO(response.body))
        names = archive.namelist()
        assert "findings.json" in names and "incidents.json" in names
        findings = json.loads(archive.read("findings.json"))
        assert any(f["rule_id"] == "HTTP-005" for f in findings)
        assert any(n.startswith("playlists/") for n in names), "the polls the findings name"
        readme = archive.read("README.txt").decode()
        assert "no longer held in memory" in readme
    finally:
        async with db_session.session_scope() as session:
            for model in (ManifestPoll, ManifestBody):
                await session.execute(delete(model).where(model.job_id == handle.id))

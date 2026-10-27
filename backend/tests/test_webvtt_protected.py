"""WebVTT on a protected ladder is read as the text it is.

A subtitle rendition inherited the ladder's protection flag. Its WebVTT segments are text in
the clear, but the parser stopped at "payload is encrypted" without reading them and the cue
checks then ran on an empty string: SUB-001, "starts with '' rather than the WEBVTT
signature", on every subtitle segment of every DRM channel.
"""

from __future__ import annotations

from app.analysis.engine import AnalysisSession, SessionOptions
from app.media.segment import analyse
from tests.fixtures.server import FixtureServer, Route, build_simple_channel

VTT = (
    "WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:900000,LOCAL:00:00:00.000\n\n"
    "00:00:01.000 --> 00:00:02.500\nHello\n\n"
    "00:00:03.000 --> 00:00:04.000 align:start\nWorld\n"
)


def test_a_webvtt_body_is_parsed_even_when_its_ladder_is_protected() -> None:
    analysis = analyse(VTT.encode(), uri="https://cdn.example/s1.vtt", encrypted=True)
    assert analysis.container == "webvtt"
    assert analysis.encrypted is False
    assert analysis.raw["webvtt_head"].startswith("WEBVTT")


def test_an_unreadable_body_behind_a_vtt_extension_stays_encrypted() -> None:
    # Whole-segment AES-128 ciphertext under a .vtt name: not text, so not read as WebVTT.
    analysis = analyse(b"\x8f\x13\x02" * 50, uri="https://cdn.example/s1.vtt", encrypted=True)
    assert analysis.encrypted is True
    assert "webvtt_head" not in analysis.raw


async def test_subtitles_on_a_protected_ladder_raise_no_sub_001(origin: FixtureServer) -> None:
    build_simple_channel(origin, variant_count=1, segment_count=4)
    origin.add_text(
        "master.m3u8",
        "#EXTM3U\n#EXT-X-VERSION:6\n"
        '#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="skd://key-1",KEYFORMAT="com.apple.streamingkeydelivery"\n'
        '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="en",URI="subs.m3u8"\n'
        "#EXT-X-STREAM-INF:BANDWIDTH=600000,RESOLUTION=640x360,FRAME-RATE=30,"
        'CODECS="avc1.64001f,mp4a.40.2",SUBTITLES="subs"\nlow.m3u8\n',
    )
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6", "#EXT-X-MEDIA-SEQUENCE:0"]
    for msn in range(4):
        lines += ["#EXTINF:6.0,", f"subs{msn}.vtt"]
        origin.add(f"subs{msn}.vtt", Route(body=VTT.encode(), content_type="text/vtt"))
    origin.add_text("subs.m3u8", "\n".join(lines) + "\n")

    session = AnalysisSession(
        session_id="vtt-drm",
        playback_url=origin.url("master.m3u8"),
        options=SessionOptions(duration_s=4.0, max_segment_samples=40),
    )
    await session.run()
    on_subs = {
        f.rule.id for f in session.collector.all() if f.variant and "sub" in f.variant.lower()
    }

    assert "SUB-001" not in on_subs, on_subs
    assert "SUB-901" in on_subs, on_subs

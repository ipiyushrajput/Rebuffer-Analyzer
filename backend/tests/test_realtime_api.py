"""Realtime API, the proxy, and the WebSocket stream."""

from __future__ import annotations

import time
from urllib.parse import quote

from fastapi.testclient import TestClient

from app.api.proxy import rewrite_playlist
from app.main import create_app
from tests.fixtures.server import FixtureServer, Route, build_simple_channel


def test_a_playlist_is_rewritten_to_route_every_uri_through_the_proxy() -> None:
    text = (
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="en",URI="audio/en.m3u8"\n'
        "#EXT-X-STREAM-INF:BANDWIDTH=1200000\n"
        "720p/index.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2400000\n"
        "https://other.example/1080p/index.m3u8?token=abc\n"
    )
    rewritten = rewrite_playlist(text, "https://edge.example/live/ch1/master.m3u8?hdnts=xyz")

    assert "/api/proxy?u=" in rewritten
    # Children resolve against the final URL's directory, not the requested one.
    assert quote("https://edge.example/live/ch1/720p/index.m3u8", safe="") in rewritten
    assert quote("https://edge.example/live/ch1/audio/en.m3u8", safe="") in rewritten
    # An absolute child keeps its own host and its query string verbatim.
    assert quote("https://other.example/1080p/index.m3u8?token=abc", safe="") in rewritten
    assert rewritten.startswith("#EXTM3U")


def test_a_key_uri_is_rewritten_too() -> None:
    text = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x0\n#EXTINF:6,\ns0.ts\n'
    rewritten = rewrite_playlist(text, "https://e/a/b/v.m3u8")
    assert quote("https://e/a/b/key.bin", safe="") in rewritten
    assert quote("https://e/a/b/s0.ts", safe="") in rewritten


def test_the_proxy_fetches_a_playlist_and_adds_cors(origin: FixtureServer) -> None:
    origin.cors_enabled = False
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    with TestClient(create_app()) as client:
        response = client.get("/api/proxy", params={"u": url})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["x-rba-playlist-kind"] == "master"
    assert "/api/proxy?u=" in response.text


def test_the_proxy_follows_a_redirect_and_reports_the_final_url(origin: FixtureServer) -> None:
    origin.add("entry.m3u8", Route(body=b"", redirect_to="/deep/master.m3u8", redirect_status=302))
    origin.add_text("deep/master.m3u8", "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n")

    with TestClient(create_app()) as client:
        response = client.get("/api/proxy", params={"u": origin.url("entry.m3u8")})

    assert response.status_code == 200
    assert response.headers["x-rba-final-url"].endswith("/deep/master.m3u8")
    assert quote(origin.url("deep/v.m3u8"), safe="") in response.text


def test_the_proxy_rejects_a_relative_url() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/proxy", params={"u": "/local/path"}).status_code == 400


def test_a_realtime_session_starts_streams_and_stops(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=2, segment_count=4)

    with TestClient(create_app()) as client:
        created = client.post(
            "/api/realtime/sessions",
            json={
                "playback_url": url,
                "channel_name": "Fixture Channel",
                "max_duration_minutes": 1,
                "options": {"ua_profile": "tizen5", "check_sets": []},
            },
        )
        assert created.status_code == 201
        body = created.json()
        session_id = body["id"]
        assert body["ws_url"] == f"/ws/realtime/{session_id}"
        assert body["player_url"].startswith("/api/proxy?u=")
        assert "analyzer host" in body["player_metrics_note"]

        with client.websocket_connect(f"/ws/realtime/{session_id}") as socket:
            kinds = set()
            deadline = time.time() + 20
            while time.time() < deadline and not {"finding", "playlist_snapshot"} <= kinds:
                message = socket.receive_json()
                kinds.add(message["type"])
                assert message["session_id"] == session_id
                assert "ts" in message
            assert "status" in kinds
            assert {"finding", "playlist_snapshot"} & kinds

            socket.send_json(
                {
                    "type": "player_event",
                    "data": {
                        "event": "stall_end",
                        "stall_duration_s": 3.2,
                        "variant": "v360p@600k",
                    },
                }
            )

        interim = client.get(f"/api/realtime/sessions/{session_id}/result").json()
        assert interim["interim"] is True
        assert "verdict" in interim
        assert isinstance(interim["findings"], list)

        manifests = client.get(f"/api/realtime/sessions/{session_id}/manifests").json()
        assert any(m["variant"] == "master" for m in manifests["manifests"])

        stopped = client.delete(f"/api/realtime/sessions/{session_id}")
        assert stopped.status_code == 200
        assert stopped.json()["status"] in ("CANCELLED", "COMPLETED")


def test_a_session_records_the_player_stall_as_a_finding(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    with TestClient(create_app()) as client:
        session_id = client.post(
            "/api/realtime/sessions",
            json={"playback_url": url, "max_duration_minutes": 1},
        ).json()["id"]

        with client.websocket_connect(f"/ws/realtime/{session_id}") as socket:
            deadline = time.time() + 15
            while time.time() < deadline:
                if socket.receive_json()["type"] == "playlist_snapshot":
                    break
            socket.send_json(
                {
                    "type": "player_event",
                    "data": {
                        "event": "stall_end",
                        "stall_duration_s": 4.1,
                        "variant": "v360p@600k",
                    },
                }
            )
            time.sleep(1.0)

        result = client.get(f"/api/realtime/sessions/{session_id}/result").json()
        assert any(f["rule_id"] == "PLY-003" for f in result["findings"])
        client.delete(f"/api/realtime/sessions/{session_id}")


def test_time_travel_returns_a_snapshot_and_its_diff(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    with TestClient(create_app()) as client:
        session_id = client.post(
            "/api/realtime/sessions", json={"playback_url": url, "max_duration_minutes": 1}
        ).json()["id"]
        time.sleep(6.0)

        snapshots = client.get(
            f"/api/realtime/sessions/{session_id}/snapshots", params={"variant": "v360p@600k"}
        )
        client.delete(f"/api/realtime/sessions/{session_id}")

    assert snapshots.status_code == 200
    body = snapshots.json()
    assert body["raw"].startswith("#EXTM3U")
    assert body["available"]
    assert "summary" in body


def test_an_unknown_session_is_a_404() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/realtime/sessions/nope").status_code == 404
        assert client.delete("/api/realtime/sessions/nope").status_code == 404


def test_an_invalid_url_or_check_set_is_rejected() -> None:
    with TestClient(create_app()) as client:
        assert (
            client.post("/api/realtime/sessions", json={"playback_url": "ftp://x"}).status_code
            == 422
        )
        assert (
            client.post(
                "/api/realtime/sessions",
                json={"playback_url": "https://x/m.m3u8", "options": {"check_sets": ["nope"]}},
            ).status_code
            == 422
        )

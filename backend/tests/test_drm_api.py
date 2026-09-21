"""The DRM endpoints: configuration, what a URL needs, and the licence relay.

The point of the whole feature is that an analyst pastes a protected playback URL into the
same box as a clear one and fills in nothing else. That only holds if three things are true,
and each is asserted here: the deployment's configuration is stored once and never read back,
a URL is probed on the backend rather than by a page that cannot reach it, and the licence
challenge is relayed by this host rather than by the browser.

Nothing here is a real credential and nothing here reaches a real key or licence server.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.fixtures.server import FixtureServer, Route

CLIENT_CERT = "/etc/rba/public_cert.pem"
CLIENT_KEY = "/etc/rba/private_key.pem"
SERVER_CERT = "/etc/rba/keyos_cert.pem"

WIDEVINE_FORMAT = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
KID = "bb3816225016413ca5de7b6dc82c92a9"

LICENCE = b"\x08\x01\x12\x20a-licence-blob-not-a-real-one--"


@pytest.fixture()
def api() -> Iterator[TestClient]:
    """A client whose DRM settings are cleared before and after, since the store outlives one test."""
    with TestClient(create_app()) as client:
        _clear(client)
        try:
            yield client
        finally:
            _clear(client)


def _clear(client: TestClient) -> None:
    response = client.put(
        "/api/drm/settings",
        json={
            "enabled": True,
            "license_url": "",
            "cpix_client_cert": "",
            "cpix_client_key": "",
            "cpix_server_cert": "",
        },
    )
    assert response.status_code == 200, response.text


def configure(client: TestClient, **overrides: Any) -> dict[str, Any]:
    body = {
        "cpix_client_cert": CLIENT_CERT,
        "cpix_client_key": CLIENT_KEY,
        "cpix_server_cert": SERVER_CERT,
        **overrides,
    }
    response = client.put("/api/drm/settings", json=body)
    assert response.status_code == 200, response.text
    return dict(response.json()["drm"])


def protected_master(server: FixtureServer, *, with_kid: bool = True) -> str:
    """A master playlist declaring Widevine, with the `pssh` box inline the way a packager does."""
    import base64

    from tests.test_drm_detect import pssh_v1

    uri = (
        f"data:text/plain;base64,{base64.b64encode(pssh_v1(KID)).decode()}"
        if with_kid
        else "skd://something"
    )
    body = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            f'#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,KEYFORMAT="{WIDEVINE_FORMAT}",'
            f'KEYFORMATVERSIONS="1",URI="{uri}"',
            "#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720",
            "high.m3u8",
            "",
        ]
    ).encode()
    server.add("/protected.m3u8", Route(body=body))
    return server.url("/protected.m3u8")


def clear_master(server: FixtureServer) -> str:
    body = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            "#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720",
            "high.m3u8",
            "",
        ]
    ).encode()
    server.add("/clear.m3u8", Route(body=body))
    server.add("/high.m3u8", Route(body=b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n"))
    return server.url("/clear.m3u8")


# -- configuration -----------------------------------------------------------


def test_the_settings_endpoint_never_returns_a_credential(api: TestClient) -> None:
    """A path can carry a hostname worth keeping off a page, and a private key's whereabouts
    is not a thing to publish. What a page is told is whether each one is set."""
    body = configure(api, license_url="https://licence.example/widevine")

    assert body["client_cert_set"] is True
    assert body["client_key_set"] is True
    assert body["server_cert_set"] is True
    assert body["keys_configured"] is True
    assert body["playback_configured"] is True

    rendered = json.dumps(api.get("/api/drm/settings").json())
    for secret in (CLIENT_CERT, CLIENT_KEY, SERVER_CERT):
        assert secret not in rendered, "the credential sources are never sent back to a page"


def test_saving_one_field_does_not_blank_the_credentials_the_page_never_saw(
    api: TestClient,
) -> None:
    """The panel is never shown the stored values, so it cannot send them back.

    A save that replaced everything it was given would wipe the credentials every time an
    operator edited the licence URL, and the next DRM channel would report no key server.
    """
    configure(api)

    response = api.put("/api/drm/settings", json={"license_url": "https://licence.example/wv"})

    assert response.status_code == 200, response.text
    body = response.json()["drm"]
    assert body["client_key_set"] is True
    assert body["license_url"] == "https://licence.example/wv"


def test_a_setting_that_is_not_a_setting_is_refused_rather_than_stored(api: TestClient) -> None:
    response = api.put("/api/drm/settings", json={"enabled": "neither"})

    assert response.status_code == 400


# -- what a playback URL needs -----------------------------------------------


def test_a_protected_url_reports_widevine_and_the_key_identifier(
    api: TestClient, origin: FixtureServer
) -> None:
    """The identifier is in the session key's own data URI, so it is known from the master
    playlist alone — before any initialisation segment has been fetched."""
    configure(api, license_url="https://licence.example/wv")

    body = api.get("/api/drm/probe", params={"url": protected_master(origin)}).json()

    assert body["protected"] is True
    assert body["system"] == "widevine"
    assert body["kid"] == KID
    assert body["player"]["key_system"] == "com.widevine.alpha"
    assert body["player"]["license_path"] == "/api/drm/license"
    assert body["player"]["configured"] is True


def test_a_protected_url_says_so_even_with_no_licence_url_configured(
    api: TestClient, origin: FixtureServer
) -> None:
    """So the page states the remedy instead of letting the player fail with an EME error."""
    body = api.get("/api/drm/probe", params={"url": protected_master(origin)}).json()

    assert body["protected"] is True
    assert body["player"]["configured"] is False


def test_a_clear_url_is_configured_as_it_always_was(api: TestClient, origin: FixtureServer) -> None:
    """Switching EME on for every stream makes a clear channel wait on a key session that
    never arrives, so a clear channel gets nothing."""
    body = api.get("/api/drm/probe", params={"url": clear_master(origin)}).json()

    assert body["protected"] is False
    assert body["player"]["key_system"] == ""
    assert body["player"]["license_path"] == ""
    assert body["player"]["configured"] is True


def test_a_url_that_does_not_answer_is_not_reported_as_clear(
    api: TestClient, origin: FixtureServer
) -> None:
    """Reporting it clear would start a protected channel with no licence and no explanation."""
    body = api.get("/api/drm/probe", params={"url": origin.url("/missing.m3u8")}).json()

    assert body["protected"] is False
    assert "404" in body["reason"], "and says what happened instead"


# -- the licence relay -------------------------------------------------------


def test_a_licence_is_relayed_and_comes_back_whole(api: TestClient, origin: FixtureServer) -> None:
    """The browser has no route to the licence server and it sends no CORS headers, so this
    host makes the request. The challenge and the licence are opaque bytes either way."""
    origin.add("/licence", Route(body=LICENCE, content_type="application/octet-stream", status=200))
    configure(api, license_url=origin.url("/licence"))

    challenge = b"\x08\x04widevine-challenge-bytes"
    response = api.post("/api/drm/license", content=challenge)

    assert response.status_code == 200
    assert response.content == LICENCE
    posted = [r for r in origin.request_log if r.get("method") == "POST"]
    assert posted and posted[-1]["body"] == challenge, "the challenge went out unaltered"


def test_a_relay_with_no_licence_url_states_the_remedy(api: TestClient) -> None:
    response = api.post("/api/drm/license", content=b"challenge")

    assert response.status_code == 409
    assert "Settings" in response.json()["detail"]


def test_an_empty_challenge_is_refused(api: TestClient, origin: FixtureServer) -> None:
    configure(api, license_url=origin.url("/licence"))

    assert api.post("/api/drm/license", content=b"").status_code == 400


def test_a_licence_server_that_refuses_is_reported_with_its_status(
    api: TestClient, origin: FixtureServer
) -> None:
    origin.add("/licence", Route(body=b"no", content_type="text/plain", status=403))
    configure(api, license_url=origin.url("/licence"))

    response = api.post("/api/drm/license", content=b"challenge")

    assert response.status_code == 502
    assert "403" in response.json()["detail"]


def test_the_relay_is_not_a_general_purpose_proxy(api: TestClient, origin: FixtureServer) -> None:
    """A licence challenge is a few kilobytes; the ceiling is what stops the endpoint being
    used to post something else through this host."""
    origin.add("/licence", Route(body=LICENCE, content_type="application/octet-stream"))
    configure(api, license_url=origin.url("/licence"))

    response = api.post("/api/drm/license", content=b"x" * (256 * 1024 + 1))

    assert response.status_code == 413

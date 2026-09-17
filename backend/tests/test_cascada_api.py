"""The CASCADA endpoints, end to end against a stand-in origin.

The unit tests next door prove the window, the query and the arithmetic. These prove the
wiring: that a call reaches CASCADA with the session attached, that the answer becomes the
body the tab reads, that a report downloads, and that an expired session comes back as one
recognisable failure rather than a different one per screen.

`app.cascada.client` is pointed at the fixture server, so nothing here touches the real
service.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from tests.fixtures.server import FixtureServer, Route

FIXTURE = Path(__file__).parent / "fixtures" / "cascada_response.json"

REALTIME_PATH = "/api/data/v1/realtime"
SESSION = "Cookie: sessionid=test-session-value; csrftoken=test-csrf"

CHANNEL = {
    "service_id": "USBA3800004EL",
    "channel_name": "Fireplace Vibes",
    "country": "US",
}


@pytest.fixture()
def cascada(origin: FixtureServer) -> Iterator[FixtureServer]:
    """The fixture server standing in for CASCADA, with the saved response on its API path."""
    settings = get_settings()
    previous = settings.cascada_base_url
    settings.cascada_base_url = origin.base_url
    origin.add(
        REALTIME_PATH,
        Route(body=FIXTURE.read_bytes(), content_type="application/json"),
    )
    try:
        yield origin
    finally:
        settings.cascada_base_url = previous


@pytest.fixture()
def api(cascada: FixtureServer) -> Iterator[TestClient]:
    """A client with no CASCADA session, since the store outlives one test."""
    with TestClient(create_app()) as test_client:
        clear_session(test_client)
        try:
            yield test_client
        finally:
            clear_session(test_client)


def store_session(api: TestClient) -> dict[str, Any]:
    response = api.put("/api/cascada/session", json={"cookie_header": SESSION})
    assert response.status_code == 200, response.text
    return dict(response.json())


def clear_session(api: TestClient) -> None:
    assert api.delete("/api/cascada/session").status_code == 200


# -- the session -------------------------------------------------------------


def test_the_session_endpoint_never_returns_the_session(api: TestClient) -> None:
    body = store_session(api)

    assert body["configured"] is True
    assert body["valid"] is True
    assert body["masked"] == "…alue"
    # The cookie itself is nowhere in the response.
    assert "test-session-value" not in json.dumps(body)


def test_a_paste_that_carries_no_sessionid_is_refused_with_the_remedy(api: TestClient) -> None:
    response = api.put("/api/cascada/session", json={"cookie_header": "_ga=GA1.2.9; foo=bar"})

    assert response.status_code == 400
    assert "devtools" in response.json()["detail"]


def test_the_session_travels_with_the_call_as_a_cookie_header(
    api: TestClient, cascada: FixtureServer
) -> None:
    store_session(api)
    cascada.clear_log()

    assert api.get("/api/cascada/channel", params=CHANNEL).status_code == 200

    sent = [entry for entry in cascada.request_log if entry["path"] == REALTIME_PATH]
    assert sent, "the call never reached CASCADA"
    assert "sessionid=test-session-value" in sent[0]["headers"]["Cookie"]


def test_a_call_with_no_session_configured_is_a_401_naming_the_settings_tab(
    api: TestClient,
) -> None:
    """A channel nothing has measured yet needs CASCADA, and CASCADA needs a session."""
    response = api.get(
        "/api/cascada/channel",
        params={"service_id": "US-NEVER-FETCHED", "channel_name": "Unknown", "country": "US"},
    )

    assert response.status_code == 401
    assert "Settings tab" in response.json()["detail"]


def test_a_window_already_stored_is_served_without_a_session(api: TestClient) -> None:
    """
    The store is a read, not a CASCADA call.

    A session that expires overnight must not blank a figure that was already measured; it
    stops new measurements, which is what the refresh path proves below.
    """
    store_session(api)
    api.get("/api/cascada/channel", params={**CHANNEL, "refresh": "true"})
    clear_session(api)

    served = api.get("/api/cascada/channel", params=CHANNEL)

    assert served.status_code == 200
    assert served.json()["cached"] is True
    # Asking for a fresh measurement still needs a session.
    assert api.get("/api/cascada/channel", params={**CHANNEL, "refresh": "true"}).status_code == 401


def test_a_login_redirect_mid_flight_reads_as_an_expired_session(
    api: TestClient, cascada: FixtureServer
) -> None:
    """Django answers an unauthenticated API call by redirecting to its login page."""
    store_session(api)
    cascada.add(
        REALTIME_PATH,
        Route(body=b"", redirect_to=f"{cascada.base_url}/accounts/login/?next=/api", status=302),
    )
    cascada.add_text("/accounts/login/", "<html>sign in</html>", content_type="text/html")

    response = api.get("/api/cascada/channel", params={**CHANNEL, "refresh": "true"})

    assert response.status_code == 401
    assert "expired" in response.json()["detail"]


def test_a_rejected_call_is_a_401_rather_than_a_gateway_error(
    api: TestClient, cascada: FixtureServer
) -> None:
    store_session(api)
    cascada.add(REALTIME_PATH, Route(body=b"denied", status=403, content_type="text/plain"))

    response = api.get("/api/cascada/channel", params={**CHANNEL, "refresh": "true"})

    assert response.status_code == 401
    assert "not accepted" in response.json()["detail"]


# -- one channel -------------------------------------------------------------


def test_a_channel_reads_back_with_both_series_and_the_figures_from_the_current_one(
    api: TestClient,
) -> None:
    store_session(api)

    body = api.get("/api/cascada/channel", params={**CHANNEL, "refresh": "true"}).json()

    assert body["service_id"] == CHANNEL["service_id"]
    assert len(body["origin"]) == 360
    assert len(body["comparison"]) == 360
    # The average is the current week's alone; the comparison rides separately.
    assert body["average_pct"] == pytest.approx(0.1245, abs=0.001)
    assert body["previous_week_average_pct"] is not None
    assert body["above_threshold"] is False
    assert body["threshold_pct"] == 0.25


def test_a_second_read_is_served_from_the_store_rather_than_from_cascada(
    api: TestClient, cascada: FixtureServer
) -> None:
    """A scan is paid for once: reopening a channel's panel must not re-hit CASCADA."""
    store_session(api)
    api.get("/api/cascada/channel", params={**CHANNEL, "refresh": "true"})
    before = cascada.hits(REALTIME_PATH)

    body = api.get("/api/cascada/channel", params=CHANNEL).json()

    assert cascada.hits(REALTIME_PATH) == before
    assert body["cached"] is True


def test_refresh_asks_cascada_again_even_when_a_stored_window_is_fresh(
    api: TestClient, cascada: FixtureServer
) -> None:
    store_session(api)
    api.get("/api/cascada/channel", params=CHANNEL)
    before = cascada.hits(REALTIME_PATH)

    api.get("/api/cascada/channel", params={**CHANNEL, "refresh": "true"})

    assert cascada.hits(REALTIME_PATH) == before + 1


def test_a_response_that_is_not_json_is_reported_as_what_arrived(
    api: TestClient, cascada: FixtureServer
) -> None:
    store_session(api)
    cascada.add_text(REALTIME_PATH, "<html>maintenance</html>", content_type="text/html")

    response = api.get("/api/cascada/channel", params={**CHANNEL, "refresh": "true"})

    assert response.status_code == 502
    assert "do not parse as JSON" in response.json()["detail"]


# -- the downloads -----------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "media"),
    [
        ("csv", "text/csv"),
        ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ],
)
def test_a_channel_report_downloads_as_an_attachment(api: TestClient, fmt: str, media: str) -> None:
    store_session(api)

    response = api.get(f"/api/cascada/channel/report.{fmt}", params=CHANNEL)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media)
    assert (
        f'filename="rebuffering_{CHANNEL["service_id"]}' in response.headers["content-disposition"]
    )
    assert response.content


def test_an_unsupported_report_format_is_refused(api: TestClient) -> None:
    store_session(api)
    response = api.get("/api/cascada/channel/report.pdf", params=CHANNEL)
    assert response.status_code == 400


# -- the scan ----------------------------------------------------------------


def test_a_scan_cannot_start_without_a_session(api: TestClient) -> None:
    """Every channel would fail the same way, so the scan says so before walking a country."""
    response = api.post("/api/cascada/scans", json={"country": "US"})

    assert response.status_code == 401
    assert "first channel" in response.json()["detail"]


def test_a_scan_of_a_country_nobody_serves_is_refused(api: TestClient) -> None:
    store_session(api)
    response = api.post("/api/cascada/scans", json={"country": "ZZ"})
    assert response.status_code == 400


def test_reading_or_cancelling_a_scan_that_does_not_exist_is_a_404(api: TestClient) -> None:
    assert api.get("/api/cascada/scans/nope").status_code == 404
    assert api.delete("/api/cascada/scans/nope").status_code == 404
    assert api.get("/api/cascada/scans/nope/report.csv").status_code == 404

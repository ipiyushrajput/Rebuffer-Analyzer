"""The TV Plus channel catalogue: URL building, dbconnect mapping and response parsing.

The catalogue is an internal service the analyzer host reaches and the browser does not, so
every property the UI depends on is asserted here rather than discovered against the live
origin: the data set each country reads in each environment, the date the search carries,
the routing marker removed from a playback URL, and where the channel rows sit in a
response whose envelope is not published.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from app.tvplus import catalogue as cat

GROUP_A = ("AU", "BR", "CA", "IN", "KR", "MX", "NZ", "TH", "US", "PH", "SG")
GROUP_B = (
    "AT", "BE", "DE", "DK", "FI", "FR", "IE", "IT", "LU", "NL",
    "NO", "PT", "ES", "SE", "CH", "GB", "EG", "SA", "AE",
)  # fmt: skip

ROW = [
    "1000",
    "US300068X7",
    "US",
    "Samsung Television Network",
    "https://d23ollwr5ob0h4.cloudfront.net/v1/master/x/HLS_CMAF_DRM/index.m3u8"
    "?ads.device_did=%7BPSID%7D&ads.service_id=US300068X7%7CCOMPONENT=HLS",
    "Y",
    "LIVE",
]
CLEAN_URL = (
    "https://d23ollwr5ob0h4.cloudfront.net/v1/master/x/HLS_CMAF_DRM/index.m3u8"
    "?ads.device_did=%7BPSID%7D&ads.service_id=US300068X7"
)


# -- the mapping -------------------------------------------------------------


def test_every_documented_country_is_selectable() -> None:
    assert set(cat.BY_CODE) == set(GROUP_A) | set(GROUP_B)
    assert all(c.name for c in cat.COUNTRIES), "every country carries a readable name"


@pytest.mark.parametrize("code", GROUP_A)
def test_group_a_reads_one_data_set_per_environment(code: str) -> None:
    assert cat.dbconnect(code, "PRD") == 1
    assert cat.dbconnect(code, "STG") == 3


@pytest.mark.parametrize("code", GROUP_B)
def test_group_b_reads_the_other_data_set_per_environment(code: str) -> None:
    assert cat.dbconnect(code, "PRD") == 0
    assert cat.dbconnect(code, "STG") == 2


def test_a_country_or_environment_outside_the_mapping_is_refused_by_name() -> None:
    with pytest.raises(cat.CatalogueError, match="not a supported country"):
        cat.dbconnect("ZZ", "PRD")
    with pytest.raises(cat.CatalogueError, match="Use PRD or STG"):
        cat.dbconnect("US", "DEV")


# -- the URL -----------------------------------------------------------------


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_the_url_carries_every_parameter_the_catalogue_expects() -> None:
    url = cat.build_url(code="AU", environment="PRD", page=1, today="20260915")
    assert url.startswith(cat.CATALOGUE_URL + "?")
    assert _query(url) == {
        "dbconnect": "1",
        "country": "AU",
        "countryCnt": "1",
        "platform": "FREESIA_PLATFORM_2023_0.3",
        "today": "20260915",
        "page": "1",
        "pageSize": "100",
    }


@pytest.mark.parametrize(
    ("code", "env", "expected"),
    [("AU", "PRD", "1"), ("IN", "STG", "3"), ("AT", "PRD", "0"), ("AT", "STG", "2")],
)
def test_the_documented_example_urls_are_reproduced(code: str, env: str, expected: str) -> None:
    query = _query(cat.build_url(code=code, environment=env, page=1, today="20260915"))
    assert query["dbconnect"] == expected
    assert query["country"] == code


def test_the_date_is_taken_when_the_search_runs_and_never_hardcoded() -> None:
    assert cat.today_stamp(dt.datetime(2026, 9, 15, tzinfo=dt.UTC)) == "20260915"
    assert cat.today_stamp(dt.datetime(2027, 1, 2, tzinfo=dt.UTC)) == "20270102"
    # No argument means now, which is what a search without a pinned date sends.
    assert _query(cat.build_url(code="US", environment="PRD"))["today"] == cat.today_stamp()


def test_a_page_below_the_first_is_refused() -> None:
    with pytest.raises(cat.CatalogueError, match="below the first page"):
        cat.build_url(code="US", environment="PRD", page=0)


# -- the routing marker ------------------------------------------------------


@pytest.mark.parametrize(
    "suffix",
    ["%7CCOMPONENT=HLS", "|COMPONENT=HLS", "%7ccomponent=HLS".replace("component", "COMPONENT")],
)
def test_the_component_marker_is_stripped_in_either_spelling(suffix: str) -> None:
    row = [*ROW[:4], f"https://cdn/x.m3u8?ads.service_id=US300068X7{suffix}", *ROW[5:]]
    assert cat.parse_row(row).playback_url == "https://cdn/x.m3u8?ads.service_id=US300068X7"


def test_a_pipe_elsewhere_in_the_query_is_left_alone() -> None:
    """Only the marker goes. A `%7C` the origin asked for is part of the URL."""
    row = [*ROW[:4], "https://cdn/x.m3u8?hdnts=a%7Cb&ads.service_id=X%7CCOMPONENT=HLS", *ROW[5:]]
    assert cat.parse_row(row).playback_url == "https://cdn/x.m3u8?hdnts=a%7Cb&ads.service_id=X"


# -- the rows ----------------------------------------------------------------


def test_a_row_maps_to_the_documented_columns() -> None:
    channel = cat.parse_row(ROW)
    assert channel.number == "1000"
    assert channel.service_id == "US300068X7"
    assert channel.country == "US"
    assert channel.name == "Samsung Television Network"
    assert channel.playback_url == CLEAN_URL
    # The undisplayed fields are kept, so adding a column later needs no parser change.
    assert channel.extra == ["Y", "LIVE"]


def test_a_short_row_parses_without_inventing_fields() -> None:
    channel = cat.parse_row(["7", "SVC", "GB", "Name", "https://cdn/x.m3u8"])
    assert channel.extra == []
    assert channel.playback_url == "https://cdn/x.m3u8"


@pytest.mark.parametrize(
    "payload",
    [
        [ROW, ROW],
        {"channels": [ROW, ROW]},
        {"result": {"list": [ROW, ROW]}},
    ],
    ids=["top-level list", "under a key", "nested under two keys"],
)
def test_rows_are_found_wherever_the_envelope_puts_them(payload: Any) -> None:
    """The envelope is not published, so rows are located by shape, not by key name."""
    assert len(cat.find_rows(payload)) == 2


def test_a_response_carrying_no_rows_is_reported_rather_than_guessed() -> None:
    with pytest.raises(cat.CatalogueError, match="no channel rows"):
        cat.find_rows({"error": "boom"})
    with pytest.raises(cat.CatalogueError, match="no channel rows"):
        cat.find_rows("not json at all")


def test_an_empty_page_is_empty_rather_than_unreadable() -> None:
    assert cat.find_rows([]) == []
    assert cat.find_rows({"channels": []}) == []


def test_a_body_that_is_not_json_names_what_arrived() -> None:
    with pytest.raises(cat.CatalogueError, match="do not parse as JSON"):
        cat.parse_page("<html>503</html>", page=1, page_size=100, url="u", today="20260915")


# -- the response the catalogue actually serves -------------------------------

LIVE_RESPONSE = (
    Path(__file__).resolve().parent / "fixtures" / "catalogue_response.json"
).read_text(encoding="utf-8")


def _live_page() -> cat.Page:
    return cat.parse_page(LIVE_RESPONSE, page=1, page_size=100, url="u", today="20260916")


def test_the_live_envelope_is_read_rows_metadata_and_pagination() -> None:
    """One page as the catalogue serves it: rows under a key, columns declared, count nested."""
    page = _live_page()
    assert len(page.channels) == 14
    assert (page.total, page.total_pages, page.has_next) == (255, 3, True)


def test_the_column_positions_come_from_the_response_rather_than_from_order() -> None:
    """`metaData` names the columns, so the tab does not trust the order they arrive in."""
    payload = json.loads(LIVE_RESPONSE)
    assert cat.column_index(payload) == {
        "number": 0,
        "service_id": 1,
        "country": 2,
        "name": 3,
        "playback_url": 4,
    }


def test_metadata_that_does_not_name_every_column_is_not_used_at_all() -> None:
    """Half a mapping would read half the fields from the wrong place."""
    assert cat.column_index({"metaData": [{"name": "CHN_NUM"}, {"name": "SVC_ID"}]}) is None
    assert cat.column_index({"rows": [ROW]}) is None


def test_a_channel_listed_with_no_playback_url_is_still_listed() -> None:
    """
    Seven of the fourteen channels on this page carry CNTN_URI null.

    Dropping them lost half the catalogue with nothing on screen to say so. They are listed,
    marked as carrying no URL, and the tab offers no action that cannot run.
    """
    page = _live_page()
    without = [c for c in page.channels if not c.analysable]
    assert len(without) == 7
    assert [c.name for c in without][:2] == ["The Bob Ross Channel", "360° Fernweh"]
    assert all(c.playback_url == "" for c in without)
    # A row with no URL still carries everything else the table shows.
    assert without[0].number == "10008"
    assert without[0].service_id == "AT16000010Y"
    assert without[0].country == "AT"


def test_every_playback_url_on_the_page_is_clean_and_usable() -> None:
    page = _live_page()
    usable = [c for c in page.channels if c.analysable]
    assert len(usable) == 7
    assert all(c.playback_url.startswith("https://") for c in usable)
    assert not any("COMPONENT=HLS" in c.playback_url for c in usable)
    # The `ads.*` parameters the analyzer needs survive; only the marker went.
    assert usable[0].playback_url.endswith("&ads.service_id=AT26000101V")


def test_a_name_the_catalogue_padded_is_trimmed_and_one_with_accents_is_kept() -> None:
    names = {c.number: c.name for c in _live_page().channels}
    assert names["4001"] == "NCIS: New Orleans"
    assert names["4063"] == "Bares für Rares"
    assert names["10067"] == "360° Fernweh"


def test_the_columns_the_tab_does_not_show_are_kept_on_the_channel() -> None:
    first = _live_page().channels[0]
    assert first.extra == ["N", "LIVE"]


# -- pagination --------------------------------------------------------------


def _page(rows: int, envelope: dict[str, Any] | None = None, page: int = 1) -> cat.Page:
    body: Any = [ROW] * rows if envelope is None else {**envelope, "channels": [ROW] * rows}
    return cat.parse_page(json.dumps(body), page=page, page_size=100, url="u", today="20260915")


def test_a_full_page_offers_the_next_one_when_the_origin_states_no_total() -> None:
    assert _page(100).has_next is True
    assert _page(100).total is None


def test_a_short_page_is_the_last_one_when_the_origin_states_no_total() -> None:
    short = _page(37)
    assert short.has_next is False
    assert short.has_previous is False
    assert len(short.channels) == 37


def test_a_total_from_the_origin_decides_the_last_page() -> None:
    first = _page(100, {"totalCount": 250}, page=1)
    assert (first.total, first.total_pages, first.has_next) == (250, 3, True)

    last = _page(50, {"totalCount": 250}, page=3)
    assert last.has_next is False
    assert last.has_previous is True


def test_a_total_page_count_from_the_origin_is_used_as_given() -> None:
    page = _page(100, {"totalPages": 2}, page=2)
    assert page.total_pages == 2
    assert page.has_next is False


def test_an_empty_page_never_offers_a_next_one() -> None:
    assert _page(0, {"totalCount": 250}, page=9).has_next is False


def test_the_page_reports_the_url_and_date_it_was_fetched_with() -> None:
    page = _page(1)
    assert page.as_dict()["today"] == "20260915"
    assert page.as_dict()["page_size"] == 100


# -- the endpoint ------------------------------------------------------------


@pytest.fixture()
def client() -> Any:
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_the_countries_endpoint_feeds_the_dropdown_without_a_second_copy(client: Any) -> None:
    """The tab holds no copy of the mapping; it asks for it."""
    body = client.get("/api/catalogue/countries").json()
    assert {c["code"] for c in body["countries"]} == set(cat.BY_CODE)
    assert body["environments"] == ["PRD", "STG"]
    assert body["page_size"] == 100


def test_an_unsupported_country_is_refused_before_any_request_goes_out(client: Any) -> None:
    response = client.get("/api/catalogue/channels", params={"country": "ZZ", "env": "PRD"})
    assert response.status_code == 400
    assert "not a supported country" in response.json()["detail"]


def test_an_origin_that_does_not_answer_is_reported_as_a_gateway_failure(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator is told the catalogue failed, not handed a traceback."""
    from app.api import catalogue as api
    from app.net.fetcher import FetchResult, Timings

    async def refuse(self: Any, url: str, **kwargs: Any) -> FetchResult:
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=0,
            headers={},
            body=b"",
            timings=Timings(),
            error="ConnectTimeout: timed out",
            transport_error=True,
        )

    monkeypatch.setattr(api.Fetcher, "fetch", refuse)
    response = client.get("/api/catalogue/channels", params={"country": "US", "env": "PRD"})
    assert response.status_code == 502
    assert "did not answer" in response.json()["detail"]
    assert "ConnectTimeout" in response.json()["detail"]


def test_a_page_is_served_with_clean_urls_and_the_date_it_used(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import catalogue as api
    from app.net.fetcher import FetchResult, Timings

    seen: dict[str, str] = {}

    async def serve(self: Any, url: str, **kwargs: Any) -> FetchResult:
        seen["url"] = url
        body = json.dumps({"totalCount": 250, "channels": [ROW] * 100}).encode()
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=200,
            headers={},
            body=body,
            timings=Timings(),
        )

    monkeypatch.setattr(api.Fetcher, "fetch", serve)
    body = client.get(
        "/api/catalogue/channels",
        params={"country": "in", "env": "stg", "page": 2, "today": "20260915"},
    ).json()

    # The search's own date and data set reach the origin.
    assert "today=20260915" in seen["url"]
    assert "dbconnect=3" in seen["url"]
    assert "page=2" in seen["url"]
    assert body["country"]["code"] == "IN"
    assert body["environment"] == "STG"
    assert body["total"] == 250
    assert body["total_pages"] == 3
    assert (body["has_previous"], body["has_next"]) == (True, True)
    assert body["channels"][0]["playback_url"] == CLEAN_URL

"""CASCADA historical rebuffering: seven complete UTC days, one value per day.

These pin what the historical source must get right and the realtime one never had to: the day
window, a columnar answer read by column name, the minimum days a verdict needs, the provider
each channel is asked for under, and a request that carries many channels at once. The scan and
the endpoints run against the fixture server standing in for CASCADA, with a real POST.

The provider list is modelled on a real `channelgroup/v1` answer: flat entries, the group name
a field of each, a channel listed again per group and per past name, switched off.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.cascada import historical, providers, service
from app.cascada.auth import _CookieAuth
from app.cascada.client import CHANNEL_GROUP_NAME, CascadaError
from app.config import Thresholds, get_settings
from app.main import create_app
from app.tvplus import catalogue as cat
from tests.fixtures.server import FixtureServer, Route

T = Thresholds()
NOW = dt.datetime(2026, 9, 23, 8, 30, tzinfo=dt.UTC)
WINDOW = historical.window_for(NOW)
COLUMNS = [
    "target_time",
    "date_category",
    "channel_id",
    "channel_name",
    "channel_country",
    "genre_name",
    "provider_name",
    "platform",
    "model",
    "rebuffering_ratio",
]
SESSION_COOKIES = {"sessionid": "test-session-value", "csrftoken": "test-csrf"}


def auth(**cookies: str) -> _CookieAuth:
    return _CookieAuth(cookies=cookies or dict(SESSION_COOKIES), source="stored")


def row(day: str, channel_id: str, value: float | None, category: str = "origin") -> list[Any]:
    return [
        day,
        category,
        channel_id,
        "Movie Hub",
        "ALL",
        "Movies",
        "NASB",
        "Samsung TV",
        "ALL",
        value,
    ]


def body(rows: list[list[Any]], columns: list[str] = COLUMNS) -> dict[str, Any]:
    return {"reference_time": {"timezone": "UTC"}, "unit": {"rebuffering_ratio": "%"},
            "columns": columns, "data": rows}  # fmt: skip


def week(channel_id: str, values: list[float | None]) -> list[list[Any]]:
    """One row per day of the window, 2026-09-16 … 2026-09-22."""
    return [
        row(day.strftime("%Y%m%d"), channel_id, value)
        for day, value in zip(WINDOW.days, values, strict=True)
    ]


# -- the window --------------------------------------------------------------


def test_the_window_is_the_seven_complete_utc_days_before_today() -> None:
    assert WINDOW.first == dt.date(2026, 9, 16)
    assert WINDOW.last == dt.date(2026, 9, 22)
    assert len(WINDOW.days) == 7
    # `to` names the last day's midnight, the day itself included — as the sample answered.
    assert WINDOW.epochs() == (1789516800, 1790035200)
    assert WINDOW.label() == "2026-09-16 → 2026-09-22"


@pytest.mark.parametrize(
    ("now", "first", "last"),
    [
        # Seconds after midnight: today has barely begun and is still excluded.
        (
            dt.datetime(2026, 9, 23, 0, 0, 5, tzinfo=dt.UTC),
            dt.date(2026, 9, 16),
            dt.date(2026, 9, 22),
        ),
        (dt.datetime(2026, 3, 3, 12, tzinfo=dt.UTC), dt.date(2026, 2, 24), dt.date(2026, 3, 2)),
        (dt.datetime(2027, 1, 2, 12, tzinfo=dt.UTC), dt.date(2026, 12, 26), dt.date(2027, 1, 1)),
        # A clock in another zone is read as the UTC moment it is.
        (
            dt.datetime(2026, 9, 23, 3, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))),
            dt.date(2026, 9, 15),
            dt.date(2026, 9, 21),
        ),
    ],
)
def test_the_window_crosses_month_year_and_zone_boundaries(
    now: dt.datetime, first: dt.date, last: dt.date
) -> None:
    window = historical.window_for(now)
    assert (window.first, window.last) == (first, last)


def test_the_payload_carries_the_constants_and_cascadas_name_for_each_channel() -> None:
    channel = historical.HistoricalChannel(
        service_id="USBA3000041ZP", provider_name="NASB_Engineering",
        cascada_name="Movie Hub", catalogue_name="The Movie Hub (US)",
    )  # fmt: skip
    sent = historical.payload(WINDOW, [channel])

    assert (sent["from"], sent["to"]) == WINDOW.epochs()
    assert sent["period_type"] == "Day"
    assert sent["channel_group_name"] == "Master Group - Local Channel Included"
    assert sent["target_metrics"] == ["rebuffering_ratio"]
    assert sent["channel_name_list"] == [
        {"provider_name": "NASB_Engineering", "channel_name": "Movie Hub",
         "channel_id": "USBA3000041ZP"}
    ]  # fmt: skip


# -- the POST ----------------------------------------------------------------


def test_the_post_carries_the_csrf_token_referer_and_origin() -> None:
    headers = historical.post_headers(auth(), base_url="https://cascada.samsungcloud.tv")

    assert headers["X-CSRFToken"] == "test-csrf"
    assert headers["Referer"] == "https://cascada.samsungcloud.tv/"
    assert headers["Origin"] == "https://cascada.samsungcloud.tv"
    assert headers["Content-Type"] == "application/json"
    assert "sessionid=test-session-value" in headers["Cookie"]


def test_the_get_headers_the_realtime_call_uses_are_unchanged() -> None:
    headers = auth().headers()
    assert "Referer" not in headers and "Origin" not in headers and "Content-Type" not in headers


def test_a_session_without_a_csrf_token_is_refused_before_any_post() -> None:
    from app.cascada.auth import CascadaAuthError

    with pytest.raises(CascadaAuthError) as caught:
        historical.post_headers(auth(sessionid="only-the-session"))
    assert "csrftoken" in str(caught.value)
    # Its own kind, so a scan does not mark a session realtime still works with as invalid.
    assert isinstance(caught.value, historical.CsrfTokenMissing)


# -- the response ------------------------------------------------------------


def test_values_are_read_by_column_name_whatever_the_order() -> None:
    reordered = list(reversed(COLUMNS))
    rows = [list(reversed(r)) for r in week("USBA3000041ZP", [0.1] * 7)]
    parsed = historical.parse(body(rows, reordered), WINDOW)

    assert list(parsed) == ["USBA3000041ZP"]
    assert all(value == pytest.approx(0.1) for value in parsed["USBA3000041ZP"].days.values())


def test_a_response_without_the_needed_columns_names_what_it_had() -> None:
    with pytest.raises(historical.HistoricalParseError) as caught:
        historical.parse(body([], ["target_time", "channel_id"]), WINDOW)
    assert "rebuffering_ratio" in str(caught.value)


def test_only_origin_rows_are_averaged_and_the_rest_are_kept_for_display() -> None:
    rows = week("C1", [0.1] * 7)
    rows += [row(day.strftime("%Y%m%d"), "C1", 9.0, "comparison") for day in WINDOW.days]
    parsed = historical.parse(body(rows), WINDOW)["C1"]
    stats = historical.summarise(parsed, T)

    assert stats.average_pct == pytest.approx(0.1)
    assert len(parsed.other) == 7
    assert {entry["category"] for entry in parsed.other} == {"comparison"}


def test_the_day_of_the_request_is_dropped_rather_than_averaged() -> None:
    """The sample answered eight rows for seven days: the eighth, today, was all null."""
    rows = [*week("C1", [0.2] * 7), row("20260923", "C1", None)]
    parsed = historical.parse(body(rows), WINDOW)["C1"]

    assert parsed.outside == 1
    assert len(parsed.days) == 7
    assert parsed.returned[-1] == dt.date(2026, 9, 22)


def test_a_null_day_is_a_gap_counted_and_never_a_zero() -> None:
    parsed = historical.parse(body(week("C1", [1.0, None, 1.0, None, 1.0, 1.0, None])), WINDOW)
    stats = historical.summarise(parsed["C1"], T)

    assert stats.average_pct == pytest.approx(1.0)
    assert stats.days_with_data == 4
    assert stats.days_expected == 7
    assert parsed["C1"].days[dt.date(2026, 9, 17)] is None


def test_a_day_cascada_returned_no_row_for_is_a_gap_too() -> None:
    rows = week("C1", [0.3] * 7)[:5]
    parsed = historical.parse(body(rows), WINDOW)["C1"]
    assert parsed.days_with_data == 5
    assert parsed.days[dt.date(2026, 9, 22)] is None


def test_below_the_minimum_days_a_channel_is_insufficient_neither_flagged_nor_passed() -> None:
    three = historical.parse(body(week("C1", [5.0, 5.0, 5.0, None, None, None, None])), WINDOW)
    stats = historical.summarise(three["C1"], T)

    assert stats.insufficient is True
    # An average far above the threshold still does not flag a channel with three days.
    assert stats.above_threshold is False

    four = historical.parse(body(week("C1", [5.0, 5.0, 5.0, 5.0, None, None, None])), WINDOW)
    assert historical.summarise(four["C1"], T).above_threshold is True


def test_the_minimum_days_is_a_setting() -> None:
    parsed = historical.parse(body(week("C1", [5.0, 5.0, None, None, None, None, None])), WINDOW)
    assert historical.summarise(parsed["C1"], T).insufficient is True
    lenient = Thresholds(cascada_historical_min_days=2)
    assert historical.summarise(parsed["C1"], lenient).insufficient is False


@pytest.mark.parametrize(("value", "flagged"), [(0.2499, False), (0.25, False), (0.2501, True)])
def test_a_steady_channel_is_flagged_only_above_the_threshold(value: float, flagged: bool) -> None:
    """The same `> 0.25` the realtime source uses, from the same constant."""
    parsed = historical.parse(body(week("C1", [value] * 7)), WINDOW)
    stats = historical.summarise(parsed["C1"], T)
    assert stats.threshold_pct == T.cascada_rebuffering_threshold_pct == 0.25
    assert stats.above_threshold is flagged


def test_a_batched_response_is_grouped_by_channel_id() -> None:
    rows = week("A", [0.1] * 7) + week("B", [0.5] * 7) + week("C", [None] * 7)
    rows.sort(key=lambda r: r[0])  # interleaved by day, as a batched answer arrives
    parsed = historical.parse(body(rows), WINDOW)

    assert set(parsed) == {"A", "B", "C"}
    assert historical.summarise(parsed["A"], T).average_pct == pytest.approx(0.1)
    assert historical.summarise(parsed["B"], T).above_threshold is True
    assert historical.summarise(parsed["C"], T).days_with_data == 0


def test_a_channel_name_with_a_raw_newline_still_parses() -> None:
    text = json.dumps(body(week("C1", [0.1] * 7))).replace("Movie Hub", "Movie\\nHub", 1)
    raw = text.replace("\\n", "\n")
    assert historical._parse_body(raw, WINDOW)["C1"].days_with_data == 7


# -- the provider map --------------------------------------------------------


def entry(
    channel_id: str, provider: str, name: str, *, group: str = CHANNEL_GROUP_NAME, on: bool = True
) -> dict[str, Any]:
    return {"channel_group_name": group, "provider_name": provider, "channel_name": name,
            "channel_id": channel_id, "channel_country": "US", "is_user_changable": False,
            "is_allow_to_viewer": False, "on_service": on}  # fmt: skip


PROVIDER_LIST = {
    "channel_list": [
        # The same channel in every group it belongs to, and under a past name, switched off.
        entry("USBA3000041ZP", "NASB_Engineering", "Movie Hub", group="Master Group"),
        entry("USBA3000041ZP", "NASB_Engineering", "Movie Hub"),
        entry("USBA3000041ZP", "NASB_Engineering", "The Movie Hub", on=False),
        entry("USBA3000041ZP", "NASB_Engineering", "Movie Hub", group="Country Group - USA"),
        # Two providers left after filtering.
        entry("AU1200004N7", "NEWID", "SMTOWN"),
        entry("AU1200004N7", "NASB_Engineering", "SMTOWN"),
        # Only in another group, or only switched off: not found.
        entry("ONLYMASTER", "P", "Other", group="Master Group"),
        entry("SWITCHEDOFF", "P", "Gone", on=False),
    ],
    "model_list": {},
}


def test_the_map_keeps_only_the_local_channel_group_and_what_is_on_service() -> None:
    mapping = providers.build_map(PROVIDER_LIST)
    found = mapping.resolve("USBA3000041ZP")

    assert found.status == "found"
    assert found.chosen is not None
    assert (found.chosen.provider_name, found.chosen.channel_name) == (
        "NASB_Engineering",
        "Movie Hub",
    )
    assert mapping.rows_read == 8


@pytest.mark.parametrize("channel_id", ["ONLYMASTER", "SWITCHEDOFF", "NEVERLISTED"])
def test_a_channel_with_no_on_service_entry_in_the_group_is_not_found(channel_id: str) -> None:
    resolution = providers.build_map(PROVIDER_LIST).resolve(channel_id)
    assert resolution.status == "not_found"
    assert resolution.chosen is None


def test_several_providers_are_chosen_between_the_same_way_every_time_and_all_listed() -> None:
    first = providers.build_map(PROVIDER_LIST).resolve("AU1200004N7")
    reversed_list = {**PROVIDER_LIST, "channel_list": list(reversed(PROVIDER_LIST["channel_list"]))}
    second = providers.build_map(reversed_list).resolve("AU1200004N7")

    assert first.status == second.status == "ambiguous"
    assert first.chosen == second.chosen
    assert first.chosen is not None and first.chosen.provider_name == "NASB_Engineering"
    assert [c.provider_name for c in first.candidates] == ["NASB_Engineering", "NEWID"]


def test_the_join_is_on_the_channel_id_never_on_a_name() -> None:
    mapping = providers.build_map(PROVIDER_LIST)
    assert mapping.resolve("Movie Hub").status == "not_found"


def test_a_provider_list_with_raw_newlines_in_names_parses() -> None:
    text = json.dumps(PROVIDER_LIST).replace('"Other"', '"line\\none"').replace("\\n", "\n")
    assert providers.parse_body(text).resolve("USBA3000041ZP").status == "found"


def test_a_provider_list_without_a_channel_list_is_refused() -> None:
    with pytest.raises(CascadaError):
        providers.build_map({"model_list": {}})


# -- batching and its fallback -----------------------------------------------


class FakeFetcher:
    """Answers each POST from a function of the channel ids it carried."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[list[str]] = []

    async def fetch(self, url: str, **kwargs: Any) -> Any:
        sent = json.loads(kwargs["content"])
        ids = [item["channel_id"] for item in sent["channel_name_list"]]
        self.calls.append(ids)
        status, payload = self.answer(ids)
        text = json.dumps(payload) if payload is not None else ""
        return SimpleNamespace(
            status=status, final_url=url, ok=200 <= status < 300, error=None, text=text
        )


def channels(*ids: str) -> list[historical.HistoricalChannel]:
    return [
        historical.HistoricalChannel(
            service_id=i, provider_name="P", cascada_name=i, catalogue_name=i
        )
        for i in ids
    ]


async def test_one_post_carries_every_channel_of_a_batch() -> None:
    fetcher = FakeFetcher(lambda ids: (200, body([r for i in ids for r in week(i, [0.1] * 7)])))
    results, failures = await historical.fetch_days(
        channels("A", "B", "C"), WINDOW, auth(), fetcher
    )  # type: ignore[arg-type]

    assert fetcher.calls == [["A", "B", "C"]]
    assert set(results) == {"A", "B", "C"}
    assert failures == {}


async def test_a_failed_batch_is_retried_channel_by_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.cascada.client.RETRY_BACKOFF_S", 0.0)

    def answer(ids: list[str]) -> tuple[int, Any]:
        if len(ids) > 1:
            return 502, None
        if ids == ["BAD"]:
            return 500, None
        return 200, body(week(ids[0], [0.1] * 7))

    fetcher = FakeFetcher(answer)
    results, failures = await historical.fetch_days(
        channels("A", "BAD", "C"), WINDOW, auth(), fetcher
    )  # type: ignore[arg-type]

    assert set(results) == {"A", "C"}
    assert set(failures) == {"BAD"}
    assert "HTTP 500" in failures["BAD"]


async def test_a_channel_missing_from_a_good_answer_is_asked_for_alone_then_named() -> None:
    fetcher = FakeFetcher(
        lambda ids: (200, body([r for i in ids if i != "GONE" for r in week(i, [0.1] * 7)]))
    )
    results, failures = await historical.fetch_days(channels("A", "GONE"), WINDOW, auth(), fetcher)  # type: ignore[arg-type]

    assert fetcher.calls == [["A", "GONE"], ["GONE"]]
    assert set(results) == {"A"}
    assert "no row" in failures["GONE"]


# -- the scan, end to end ----------------------------------------------------

HISTORICAL_PATH = "/api/data/v1/historical"


@pytest.fixture()
def cascada(origin: FixtureServer) -> Iterator[FixtureServer]:
    """CASCADA's historical endpoint, answering from the channel ids each POST carried."""

    def answer(_query: dict[str, list[str]]) -> bytes:
        sent = json.loads(origin.request_log[-1]["body"])
        values = {"USBA3000041ZP": 0.5, "AU1200004N7": 0.1, "THIN": None}
        rows: list[list[Any]] = []
        for item in sent["channel_name_list"]:
            cid = item["channel_id"]
            daily = (
                [values[cid]] * 7
                if values.get(cid) is not None
                else [0.9, None, None, None, None, None, None]
            )
            rows += week(cid, daily)
        rows.append(row("20260923", "USBA3000041ZP", None))
        return json.dumps(body(rows)).encode()

    settings = get_settings()
    previous = settings.cascada_base_url
    settings.cascada_base_url = origin.base_url
    origin.add(HISTORICAL_PATH, Route(body=answer, content_type="application/json"))
    try:
        yield origin
    finally:
        settings.cascada_base_url = previous


@pytest.fixture()
def scan_world(cascada: FixtureServer, monkeypatch: pytest.MonkeyPatch) -> Iterator[FixtureServer]:
    listed = [
        cat.Channel(number="1", service_id="USBA3000041ZP", country="US", name="The Movie Hub",
                    playback_url="https://example.invalid/a.m3u8", extra=[]),
        cat.Channel(number="2", service_id="AU1200004N7", country="US", name="SMTOWN",
                    playback_url="https://example.invalid/b.m3u8", extra=[]),
        cat.Channel(number="3", service_id="THIN", country="US", name="Thin",
                    playback_url="https://example.invalid/c.m3u8", extra=[]),
        # The catalogue can publish a channel twice; it is asked for and judged once.
        cat.Channel(number="1", service_id="USBA3000041ZP", country="US", name="The Movie Hub",
                    playback_url="https://example.invalid/a.m3u8", extra=[]),
        cat.Channel(number="4", service_id="NOPROVIDER", country="US", name="Orphan",
                    playback_url="", extra=[]),
    ]  # fmt: skip

    async def fake_channels(country: str) -> list[cat.Channel]:
        return listed

    monkeypatch.setattr(service, "country_channels", fake_channels)
    mapping = providers.build_map(
        {
            **PROVIDER_LIST,
            "channel_list": [*PROVIDER_LIST["channel_list"], entry("THIN", "P", "Thin")],
        }
    )
    providers.set_cached(mapping)
    try:
        yield cascada
    finally:
        providers.set_cached(None)


async def _finished(scan: service.Scan) -> service.Scan:
    import asyncio

    for _ in range(200):
        if scan.finished_at is not None:
            return scan
        await asyncio.sleep(0.05)
    raise AssertionError("the scan did not finish")


def test_a_historical_scan_judges_lists_and_labels_every_channel(
    scan_world: FixtureServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_auth() -> _CookieAuth:
        return auth()

    monkeypatch.setattr(service, "resolve_auth", fake_auth)
    # The scan asks for the last seven days before today; today is frozen to the fixture's.
    monkeypatch.setattr(historical, "window_for", lambda *args, **kwargs: WINDOW)
    with TestClient(create_app()) as api:
        scan = api.portal.call(lambda: service.start_scan("US", 1, source="historical"))  # type: ignore[attr-defined]
        scan = api.portal.call(_finished, scan)  # type: ignore[attr-defined]

    assert scan.status == "COMPLETED", scan.error
    assert scan.source == "historical"
    # Judged: the flagged one above, the clean one below.
    assert [w.service_id for w in scan.above] == ["USBA3000041ZP"]
    assert set(scan.results) == {"USBA3000041ZP", "AU1200004N7"}
    # Not judged, and named: one day of data, and no provider.
    assert [row["service_id"] for row in scan.insufficient] == ["THIN"]
    assert [row["service_id"] for row in scan.no_provider] == ["NOPROVIDER"]
    assert scan.ambiguous[0]["candidates"] == ["NASB_Engineering", "NEWID"]
    # The label is the days returned, and today's null row was dropped rather than counted.
    assert scan.window_label == "2026-09-16 → 2026-09-22"
    assert scan.done == scan.total == 5

    posts = [e for e in scan_world.request_log if e["path"] == HISTORICAL_PATH]
    assert len(posts) == 1, "three channels with a provider go in one POST"
    sent = json.loads(posts[0]["body"])
    assert len(sent["channel_name_list"]) == 3, "a channel listed twice is sent once"
    assert {c["channel_id"] for c in sent["channel_name_list"]} == {
        "USBA3000041ZP",
        "AU1200004N7",
        "THIN",
    }
    # CASCADA's own name goes in the payload, not the catalogue's.
    assert {"provider_name": "NASB_Engineering", "channel_name": "Movie Hub",
            "channel_id": "USBA3000041ZP"} in sent["channel_name_list"]  # fmt: skip
    assert posts[0]["headers"]["X-CSRFToken"] == "test-csrf"
    assert posts[0]["headers"]["Referer"].endswith("/")

    payload = scan.as_dict()
    assert payload["source"] == "historical"
    assert payload["insufficient_count"] == 1
    assert payload["above"][0]["granularity"] == "day"


# -- window arithmetic over the frozen clock ---------------------------------


def test_the_window_label_of_a_realtime_scan_states_its_rolling_minutes() -> None:
    scan = service.Scan(
        id="x", country="US", total=0, threshold_pct=0.25,
        window=service.client.window_for(T, NOW),
    )  # fmt: skip
    assert scan.window_label == "2026-09-16 00:00 → 2026-09-23 08:30 UTC"


# -- the endpoints -----------------------------------------------------------


@pytest.fixture()
def endpoints(scan_world: FixtureServer, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def fake_auth() -> _CookieAuth:
        return auth()

    monkeypatch.setattr(historical, "resolve_auth", fake_auth)
    monkeypatch.setattr(historical, "window_for", lambda *args, **kwargs: WINDOW)
    with TestClient(create_app()) as api:
        yield api


def test_one_channels_days_are_served_with_their_provider_and_dates(endpoints: TestClient) -> None:
    response = endpoints.get(
        "/api/cascada/channel/historical",
        params={"service_id": "USBA3000041ZP", "channel_name": "The Movie Hub", "country": "US",
                "refresh": "true"},
    )  # fmt: skip
    assert response.status_code == 200, response.text
    body_ = response.json()

    assert body_["source"] == "historical"
    assert body_["granularity"] == "day"
    assert len(body_["origin"]) == 7
    assert body_["days_with_data"] == 7
    assert body_["above_threshold"] is True
    assert body_["returned_days"]["label"] == "2026-09-16 → 2026-09-22"
    assert body_["provider_name"] == "NASB_Engineering"
    assert body_["cascada_channel_name"] == "Movie Hub"
    # The catalogue's name is what the product shows.
    assert body_["channel_name"] == "The Movie Hub"


def test_the_days_download_as_one_row_per_day(endpoints: TestClient) -> None:
    params = {"service_id": "USBA3000041ZP", "channel_name": "The Movie Hub", "country": "US"}
    text = endpoints.get("/api/cascada/channel/historical/report.csv", params=params).text
    lines = text.splitlines()

    assert lines[0] == "day_utc,rebuffering_ratio_pct,above_threshold"
    assert lines[1] == "2026-09-16,0.5000,yes"
    assert lines[7].startswith("2026-09-22,")
    assert "historical" in text


def test_a_channel_with_no_provider_is_a_404_that_says_so(endpoints: TestClient) -> None:
    response = endpoints.get(
        "/api/cascada/channel/historical",
        params={"service_id": "NOPROVIDER", "channel_name": "Orphan", "country": "US"},
    )
    assert response.status_code == 404
    assert "Provider not found" in response.json()["detail"]


def test_the_provider_map_reports_what_it_holds(endpoints: TestClient) -> None:
    held = endpoints.get("/api/cascada/providers").json()
    assert held["loaded"] is True
    assert held["group"] == "Master Group - Local Channel Included"
    assert held["ambiguous_channels"] == 1

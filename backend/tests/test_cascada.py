"""CASCADA rebuffering data: the window, the query, the two series and what is averaged.

CASCADA is an internal service the analyzer reaches and no test can call, so every property
the tab depends on is asserted here against a saved response: which rows are current and
which are last week's, that a comparison row never reaches an average, that a gap is a gap
rather than a zero, and that one bad minute does not make a channel a rebuffering channel.

`tests/fixtures/cascada_response.json` is a real response with its two series trimmed to six
hours each. Its structure — the `reference_time` block, the `unit` map, the `date_category`
field — is untouched.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from app.cascada import auth, client, exports, series, store
from app.config import Thresholds

FIXTURE = Path(__file__).parent / "fixtures" / "cascada_response.json"

T = Thresholds()
BASE = "https://cascada.samsungcloud.tv"

# The window the saved response was fetched for.
ORIGIN_START = dt.datetime(2026, 9, 14, 0, 0, tzinfo=dt.UTC)
ORIGIN_END = dt.datetime(2026, 9, 14, 5, 59, tzinfo=dt.UTC)


def payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def window(start: dt.datetime, end: dt.datetime) -> series.Window:
    return series.Window(start=start, end=end)


def fixture_window() -> series.Window:
    return window(ORIGIN_START, ORIGIN_END)


def flat(minutes: int, value: float | None, *, category: str, start: dt.datetime) -> list[Any]:
    """A synthetic series of one repeated value, for the averaging cases."""
    return [
        {
            "target_time": (start + dt.timedelta(minutes=n)).isoformat(),
            "date_category": category,
            "rebuffering_ratio": value,
        }
        for n in range(minutes)
    ]


# -- the window --------------------------------------------------------------


def test_the_window_starts_at_midnight_a_week_before_today_and_ends_at_this_minute() -> None:
    """Pinning the start to midnight makes two calls an hour apart cover the same days."""
    now = dt.datetime(2026, 9, 17, 5, 13, 42, 500000, tzinfo=dt.UTC)
    span = client.window_for(T, now)

    assert span.start == dt.datetime(2026, 9, 10, 0, 0, tzinfo=dt.UTC)
    assert span.end == dt.datetime(2026, 9, 17, 5, 13, tzinfo=dt.UTC)
    # The seconds are dropped, so the same minute always produces the same query.
    assert span.end.second == 0 and span.end.microsecond == 0


def test_the_window_crosses_a_month_boundary_without_arithmetic_of_its_own() -> None:
    now = dt.datetime(2026, 3, 4, 11, 30, tzinfo=dt.UTC)
    span = client.window_for(T, now)
    assert span.start == dt.datetime(2026, 2, 25, 0, 0, tzinfo=dt.UTC)


def test_the_window_is_taken_in_utc_whatever_zone_the_host_reports() -> None:
    """A host in KST must not shift the window by nine hours."""
    kst = dt.timezone(dt.timedelta(hours=9))
    now = dt.datetime(2026, 9, 17, 8, 30, tzinfo=kst)  # 2026-09-16T23:30Z
    span = client.window_for(T, now)

    assert span.end == dt.datetime(2026, 9, 16, 23, 30, tzinfo=dt.UTC)
    assert span.start == dt.datetime(2026, 9, 9, 0, 0, tzinfo=dt.UTC)


def test_the_epochs_in_the_query_are_whole_seconds_utc() -> None:
    span = window(
        dt.datetime(2026, 9, 14, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 9, 17, 5, 13, tzinfo=dt.UTC),
    )
    query = parse_qs(
        urlsplit(
            client.build_url(
                channel_name="Fireplace Vibes",
                channel_id="USBA3800004EL",
                window=span,
                limit=100,
                base_url=BASE,
            )
        ).query
    )

    assert query["from"] == ["1789344000"]
    assert query["to"] == ["1789621980"]


def test_the_row_limit_is_computed_from_the_window_rather_than_fixed() -> None:
    """
    A week of per-minute data plus its comparison week is far more rows than 14713.

    The documented constant was written for a three-day window; a seven-day one needs room
    for both series or the average silently covers part of the week.
    """
    span = client.window_for(T, dt.datetime(2026, 9, 17, 5, 13, tzinfo=dt.UTC))
    limit = client.row_limit(span, margin=0)

    assert limit == span.minutes + 7 * 1440
    assert limit > 14713
    # The margin is headroom for the window growing between the request and the answer.
    assert client.row_limit(span, margin=600) == limit + 600


# -- the query ---------------------------------------------------------------


def test_the_query_carries_every_constant_and_encodes_the_bracketed_parameter() -> None:
    url = client.build_url(
        channel_name="Fireplace Vibes",
        channel_id="USBA3800004EL",
        window=fixture_window(),
        limit=14713,
        base_url=BASE,
    )
    query = parse_qs(urlsplit(url).query)

    assert urlsplit(url).path == "/api/data/v1/realtime"
    assert query["channel_group_name"] == ["Master Group - Local Channel Included"]
    assert query["channel_name"] == ["Fireplace Vibes"]
    assert query["channel_id"] == ["USBA3800004EL"]
    assert query["channel_country"] == ["ALL"]
    assert query["platform"] == ["Samsung TV"]
    assert query["model"] == ["ALL"]
    assert query["compare_with"] == ["1WEEK"]
    assert query["target_metrics[]"] == ["rebuffering_ratio"]
    assert query["export_to_gcs"] == ["true"]

    raw = urlsplit(url).query
    # Spaces travel as `+` and the brackets percent-encoded, which is what the dashboard sends.
    assert "channel_group_name=Master+Group+-+Local+Channel+Included" in raw
    assert "platform=Samsung+TV" in raw
    assert "target_metrics%5B%5D=rebuffering_ratio" in raw


def test_a_channel_name_with_punctuation_survives_the_round_trip() -> None:
    """The name is the catalogue's, not ours, and CASCADA matches on it exactly."""
    name = "AFV: Pets & Animals (Español) #2"
    url = client.build_url(
        channel_name=name, channel_id="US1000", window=fixture_window(), limit=10, base_url=BASE
    )
    assert parse_qs(urlsplit(url).query)["channel_name"] == [name]


# -- the two series ----------------------------------------------------------


def test_the_response_splits_into_the_current_window_and_the_week_before_it() -> None:
    parsed = series.parse(payload(), requested=fixture_window())

    assert parsed.categories == {"origin": 360, "comparison": 360}
    assert parsed.unexpected_categories == []
    assert len(parsed.origin) == 360
    assert len(parsed.comparison) == 360
    # The comparison rows are exactly one week before the current ones.
    assert parsed.comparison[0].at == parsed.origin[0].at - dt.timedelta(days=7)
    assert parsed.unit == "%"


def test_the_measured_window_is_read_from_the_rows_rather_than_assumed() -> None:
    parsed = series.parse(payload(), requested=fixture_window())
    measured = parsed.measured

    assert measured is not None
    assert (measured.start, measured.end) == (ORIGIN_START, ORIGIN_END)


def test_a_comparison_row_never_reaches_the_average() -> None:
    """
    Mixing the two series would average a fortnight into a figure labelled as this week.

    The current week here is clean and the previous week is not; an average that moved would
    mean comparison rows had been counted.
    """
    start = dt.datetime(2026, 9, 10, tzinfo=dt.UTC)
    rows = flat(60, 0.10, category="origin", start=start)
    rows += flat(60, 9.00, category="comparison", start=start - dt.timedelta(days=7))

    parsed = series.parse({"data": rows}, requested=window(start, start + dt.timedelta(minutes=59)))
    stats = series.summarise(parsed, T)

    assert stats.average_pct == pytest.approx(0.10)
    assert stats.above_threshold is False
    # Last week is still reported — it is the whole reason those rows are fetched.
    assert stats.previous_week_average_pct == pytest.approx(9.00)
    assert stats.week_over_week_delta_pct == pytest.approx(-8.90)


def test_a_row_in_a_category_nobody_has_seen_is_reported_rather_than_dropped() -> None:
    start = dt.datetime(2026, 9, 10, tzinfo=dt.UTC)
    rows = flat(5, 0.1, category="origin", start=start)
    rows += flat(5, 0.2, category="forecast", start=start)

    parsed = series.parse({"data": rows}, requested=window(start, start))
    assert parsed.unexpected_categories == ["forecast"]
    # It is counted, so a reader can see how much data went nowhere.
    assert parsed.categories["forecast"] == 5


def test_a_response_with_no_data_list_is_refused_with_what_did_arrive() -> None:
    with pytest.raises(series.CascadaParseError) as caught:
        series.parse({"reference_time": {}, "unit": {}}, requested=fixture_window())
    assert "reference_time" in str(caught.value)


# -- averaging, gaps and the threshold ---------------------------------------


def test_a_minute_with_no_measurement_is_a_gap_and_not_a_zero() -> None:
    """
    A zero would state the channel rebuffered for none of that minute, which was not measured.

    Ten measured minutes at 1.0 and ten unmeasured ones average 1.0, not 0.5.
    """
    start = dt.datetime(2026, 9, 10, tzinfo=dt.UTC)
    rows = flat(10, 1.0, category="origin", start=start)
    rows += flat(10, None, category="origin", start=start + dt.timedelta(minutes=10))

    stats = series.summarise(series.parse({"data": rows}, requested=window(start, start)), T)

    assert stats.average_pct == pytest.approx(1.0)
    assert stats.minutes_counted == 10
    assert stats.minutes_missing == 10


@pytest.mark.parametrize("raw", [None, "", "n/a", [], {}])
def test_every_shape_of_missing_value_reads_as_a_gap(raw: Any) -> None:
    rows = [
        {"target_time": "2026-09-10T00:00:00+00:00", "date_category": "origin",
         "rebuffering_ratio": raw},
    ]  # fmt: skip
    parsed = series.parse({"data": rows}, requested=fixture_window())
    assert parsed.origin[0].value is None


def test_a_channel_with_one_spike_and_a_clean_week_is_not_a_rebuffering_channel() -> None:
    """
    The average alone qualifies a channel. This is the distinction the scan exists to make.

    One minute at 4% inside a week at 0.05% averages 0.0095%, far below the threshold, and a
    channel is not escalated for a single bad minute.
    """
    start = dt.datetime(2026, 9, 10, tzinfo=dt.UTC)
    rows = flat(419, 0.05, category="origin", start=start)
    rows.append(
        {
            "target_time": (start + dt.timedelta(minutes=419)).isoformat(),
            "date_category": "origin",
            "rebuffering_ratio": 4.0,
        }
    )

    stats = series.summarise(series.parse({"data": rows}, requested=window(start, start)), T)

    assert stats.max_pct == pytest.approx(4.0)
    assert stats.minutes_above == 1
    assert stats.average_pct is not None and stats.average_pct < T.cascada_rebuffering_threshold_pct
    assert stats.above_threshold is False


def test_a_channel_whose_average_is_above_the_threshold_does_qualify() -> None:
    start = dt.datetime(2026, 9, 10, tzinfo=dt.UTC)
    rows = flat(120, 0.40, category="origin", start=start)

    stats = series.summarise(series.parse({"data": rows}, requested=window(start, start)), T)

    assert stats.average_pct == pytest.approx(0.40)
    assert stats.above_threshold is True
    assert stats.minutes_above == 120
    assert stats.percent_time_above == pytest.approx(100.0)


def test_the_threshold_is_a_percentage_and_not_the_virtual_buffer_ratio() -> None:
    """
    CASCADA reports percent; `rebuffer_ratio_threshold` is a fraction. They differ a hundredfold.

    Comparing a CASCADA figure against the ratio threshold would call a channel rebuffering
    at 24% of viewing time clean, so the two thresholds are separate numbers.
    """
    assert T.cascada_rebuffering_threshold_pct == 0.25
    assert series.is_above(0.26, T) is True
    assert series.is_above(0.25, T) is False
    assert series.is_above(None, T) is False

    # The two defaults read the same and mean a hundredfold apart. A channel measured at
    # 0.30% is above the CASCADA threshold; the same measurement as a fraction is 0.003,
    # nowhere near the ratio threshold, so judging it by that number would call it clean.
    measured_pct = 0.30
    assert series.is_above(measured_pct, T) is True
    assert measured_pct / 100 < T.rebuffer_ratio_threshold


def test_the_peak_carries_the_minute_it_happened_in() -> None:
    parsed = series.parse(payload(), requested=fixture_window())
    stats = series.summarise(parsed, T)

    assert stats.max_pct == pytest.approx(0.201)
    assert stats.max_at is not None
    assert ORIGIN_START <= stats.max_at <= ORIGIN_END


def test_the_saved_response_averages_what_its_current_week_rows_average() -> None:
    parsed = series.parse(payload(), requested=fixture_window())
    stats = series.summarise(parsed, T)
    values = [p.value for p in parsed.origin if p.value is not None]

    assert stats.average_pct == pytest.approx(sum(values) / len(values))
    assert stats.above_threshold is False


# -- truncation --------------------------------------------------------------


def test_a_series_shorter_than_the_window_asked_for_is_reported_as_truncated() -> None:
    """A partial week must never be presented as a whole one."""
    asked = window(ORIGIN_START, ORIGIN_START + dt.timedelta(days=7))
    parsed = series.parse(payload(), requested=asked)

    assert parsed.truncated is True
    assert len(parsed.origin) < asked.minutes


def test_a_series_covering_the_whole_window_is_not_truncated() -> None:
    parsed = series.parse(payload(), requested=fixture_window())
    assert parsed.requested.minutes == 360
    assert parsed.truncated is False


# -- the session -------------------------------------------------------------


def test_a_pasted_cookie_header_is_read_and_everything_else_is_dropped() -> None:
    """Devtools' "Copy as cURL" gives the whole header, which is one copy and one paste."""
    pasted = (
        "Cookie: _ga=GA1.2.999; sessionid=abc123def456; csrftoken=tok789; "
        "messages=hello; ajs_anonymous_id=xyz"
    )
    cookies = auth.parse_cookie_header(pasted)

    assert cookies == {"sessionid": "abc123def456", "csrftoken": "tok789", "messages": "hello"}
    # Analytics cookies are not forwarded to CASCADA.
    assert "_ga" not in auth.cookie_header(cookies)


@pytest.mark.parametrize(
    "pasted",
    [
        "sessionid=abc123def456; csrftoken=tok789",
        "'sessionid=abc123def456; csrftoken=tok789'",
        "  cookie:   sessionid=abc123def456;csrftoken=tok789  ",
    ],
)
def test_the_session_reads_from_every_shape_an_operator_pastes(pasted: str) -> None:
    assert auth.parse_cookie_header(pasted)["sessionid"] == "abc123def456"


def test_a_call_with_no_session_says_what_to_do_rather_than_failing_obscurely() -> None:
    provider = auth._CookieAuth(cookies={}, source="stored")

    with pytest.raises(auth.CascadaAuthError) as caught:
        provider.headers()
    assert "Settings tab" in str(caught.value)
    assert provider.describe().configured is False


def test_the_session_is_masked_everywhere_it_is_shown() -> None:
    """The cookie is never returned by an endpoint; only enough to tell two sessions apart."""
    provider = auth._CookieAuth(cookies={"sessionid": "abc123def456"}, source="stored")
    described = provider.describe().as_dict()

    assert described["masked"] == "…f456"
    assert "abc123def456" not in json.dumps(described)


@pytest.mark.parametrize(
    ("status", "final_url"),
    [
        (401, f"{BASE}/api/data/v1/realtime"),
        (403, f"{BASE}/api/data/v1/realtime"),
        (302, f"{BASE}/accounts/login/?next=/api/data/v1/realtime"),
        (200, f"{BASE}/sso/authorize"),
    ],
)
def test_a_rejected_or_redirected_call_is_an_expired_session(status: int, final_url: str) -> None:
    reason = client._auth_failure(status, final_url)
    assert reason is not None
    assert "session" in reason.lower()


def test_a_served_api_response_is_not_mistaken_for_a_login_page() -> None:
    assert client._auth_failure(200, f"{BASE}/api/data/v1/realtime?limit=10") is None


# -- the reports -------------------------------------------------------------


def entry(
    *, service_id: str, name: str, average: float, above: bool, points: int = 3
) -> store.ChannelWindow:
    start = dt.datetime(2026, 9, 10, tzinfo=dt.UTC)
    origin = [
        series.Point(at=start + dt.timedelta(minutes=n), value=average) for n in range(points)
    ]
    stats = series.Stats(
        average_pct=average,
        max_pct=average,
        max_at=start,
        minutes_above=points if above else 0,
        minutes_counted=points,
        minutes_missing=0,
        percent_time_above=100.0 if above else 0.0,
        previous_week_average_pct=average / 2,
        threshold_pct=T.cascada_rebuffering_threshold_pct,
        above_threshold=above,
    )
    return store.ChannelWindow(
        service_id=service_id,
        channel_name=name,
        country="IN",
        window=window(start, start + dt.timedelta(minutes=points - 1)),
        stats=stats,
        origin=origin,
        comparison=[],
        fetched_at=start,
        truncated=False,
    )


def test_the_country_report_lists_only_channels_above_the_threshold_worst_first() -> None:
    rows = [
        entry(service_id="IN2", name="Second", average=0.90, above=True),
        entry(service_id="IN1", name="Worst", average=2.40, above=True),
        entry(service_id="IN3", name="Clean", average=0.01, above=False),
    ]
    above = sorted(
        [row for row in rows if row.stats.above_threshold],
        key=lambda w: w.stats.average_pct or 0.0,
        reverse=True,
    )
    text = exports.country_csv(
        above,
        country="IN",
        window=fixture_window(),
        threshold_pct=T.cascada_rebuffering_threshold_pct,
        partial=False,
        scanned="3 of 3 channel(s) measured, 0 failed, status COMPLETED",
    )

    lines = text.splitlines()

    assert "Clean" not in text
    # Row one is the header, so a spreadsheet reads the file as the table it is.
    assert lines[0] == ",".join(exports.COUNTRY_COLUMNS)
    assert lines[1].startswith("Worst,")
    assert lines[2].startswith("Second,")
    # The window and the threshold still ride with the figures, below them.
    assert ORIGIN_START.isoformat() in text
    assert "threshold_pct,0.2500" in text


def test_an_unfinished_scan_says_so_in_the_report_it_produces() -> None:
    text = exports.country_csv(
        [entry(service_id="IN1", name="Worst", average=2.4, above=True)],
        country="IN",
        window=fixture_window(),
        threshold_pct=T.cascada_rebuffering_threshold_pct,
        partial=True,
        scanned="42 of 310 channel(s) measured, 0 failed, status RUNNING",
    )
    assert "had not finished" in text
    assert "42 of 310" in text


def test_the_country_report_states_that_the_figures_are_not_per_country() -> None:
    """`channel_country=ALL`, so the figure is the channel's rebuffering everywhere."""
    text = exports.country_csv(
        [entry(service_id="IN1", name="Worst", average=2.4, above=True)],
        country="IN",
        window=fixture_window(),
        threshold_pct=T.cascada_rebuffering_threshold_pct,
        partial=False,
        scanned="done",
    )
    assert "channel_country=ALL" in text


def test_the_channel_report_opens_with_its_header_row_then_one_row_per_minute() -> None:
    row = entry(service_id="IN1", name="Worst", average=0.40, above=True, points=5)
    lines = exports.channel_csv(row).splitlines()

    assert lines[0] == "timestamp_utc,rebuffering_ratio_pct,above_threshold"
    assert lines[1].endswith(",0.4000,yes")
    assert len([line for line in lines[1:6] if line]) == 5
    # The context follows the minutes rather than pushing the header down the sheet.
    assert "average_pct,0.4000" in "\n".join(lines)
    assert "minutes_above_threshold,5" in "\n".join(lines)


def test_a_minute_with_no_measurement_leaves_an_empty_cell_not_a_zero() -> None:
    row = entry(service_id="IN1", name="Gappy", average=0.40, above=True, points=2)
    row.origin = [*row.origin, series.Point(at=ORIGIN_END, value=None)]
    lines = exports.channel_csv(row).splitlines()

    # Three minutes follow the header; the last was never measured.
    assert lines[3] == f"{ORIGIN_END.isoformat()},,"


@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
def test_the_report_filename_names_the_country_and_the_day(fmt: str) -> None:
    name = exports.country_filename("in", fmt, dt.datetime(2026, 9, 17, tzinfo=dt.UTC))
    assert name == f"rebuffering_report_IN_20260917.{fmt}"


def test_the_country_workbook_parses_back_with_its_rows_in_order() -> None:
    from openpyxl import load_workbook

    rows = [
        entry(service_id="IN1", name="Worst", average=2.40, above=True),
        entry(service_id="IN2", name="Second", average=0.90, above=True),
    ]
    data = exports.country_xlsx(
        rows,
        country="IN",
        window=fixture_window(),
        threshold_pct=T.cascada_rebuffering_threshold_pct,
        partial=False,
        scanned="done",
    )
    workbook = load_workbook(io.BytesIO(data))
    sheet = workbook["rebuffering"]
    header = [str(cell.value) for cell in next(sheet.iter_rows(max_row=1))]
    names = [str(row[0].value) for row in sheet.iter_rows(min_row=2)]

    # The columns run left to right on row one; the channels run down beneath them.
    assert header == list(exports.COUNTRY_COLUMNS)
    assert names == ["Worst", "Second"]
    # The context sits on its own sheet rather than above the table.
    assert exports.ABOUT_SHEET in workbook.sheetnames
    assert sheet.freeze_panes == "A2"


def test_the_channel_workbook_keeps_the_summary_and_the_minutes_on_separate_sheets() -> None:
    from openpyxl import load_workbook

    data = exports.channel_xlsx(entry(service_id="IN1", name="Worst", average=0.4, above=True))
    workbook = load_workbook(io.BytesIO(data))

    assert workbook.sheetnames == ["minutes", exports.ABOUT_SHEET]
    assert workbook["minutes"].max_row == 4  # one header plus three minutes


def test_the_country_report_carries_the_playback_url_for_each_channel() -> None:
    """
    A report that names a rebuffering channel but not where to fetch it is half an answer.

    The URL is the catalogue's, carried through the scan, so the report is enough on its own
    to hand a channel to whoever has to analyse it.
    """
    url = "https://cdn.example/live/worst/index.m3u8?ads.service_id=IN1"
    text = exports.country_csv(
        [entry(service_id="IN1", name="Worst", average=2.4, above=True)],
        country="IN",
        window=fixture_window(),
        threshold_pct=T.cascada_rebuffering_threshold_pct,
        partial=False,
        scanned="done",
        urls={"IN1": url},
    )
    header, first = text.splitlines()[:2]

    assert "playback_url" in header.split(",")
    assert url in first


def test_a_channel_the_catalogue_gives_no_url_for_leaves_the_cell_empty() -> None:
    """It is still rebuffering, so it belongs in the report; there is just nothing to fetch."""
    text = exports.country_csv(
        [entry(service_id="IN2", name="No URL", average=2.4, above=True)],
        country="IN",
        window=fixture_window(),
        threshold_pct=T.cascada_rebuffering_threshold_pct,
        partial=False,
        scanned="done",
        urls={},
    )
    columns = text.splitlines()[1].split(",")

    assert columns[0] == "No URL"
    assert columns[exports.COUNTRY_COLUMNS.index("playback_url")] == ""

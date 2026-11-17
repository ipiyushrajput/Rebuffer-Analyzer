"""The automated batch: its settings, its report, and the endpoints that drive it.

The pipeline itself is exercised end to end in `test_batch_pipeline.py`, against the fixture
origin. What is asserted here is everything a reader of the report depends on — the column
order, the window it claims, the channels it must not omit — and the rules that decide which
channels get into it at all.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.batch import exports, store, summary
from app.batch.settings import DEFAULTS, BatchSettings
from app.cascada.series import Stats
from app.config import Thresholds
from app.main import create_app

WINDOW_FROM = dt.datetime(2026, 9, 11, 0, 0, tzinfo=dt.UTC)
WINDOW_TO = dt.datetime(2026, 9, 18, 0, 0, tzinfo=dt.UTC)


@pytest.fixture()
def api() -> TestClient:
    with TestClient(create_app()) as client:
        yield client


def item(
    *,
    service_id: str,
    name: str,
    average: float,
    status: str = "ANALYSED",
    text: str = "",
    error: str | None = None,
    correlation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "service_id": service_id,
        "channel_name": name,
        "country": "GB",
        "playback_url": f"https://cdn.example/live/{service_id}/index.m3u8",
        "average_pct": average,
        "max_pct": average * 2,
        "minutes_above": 42,
        "status": status,
        "job_id": f"job-{service_id}",
        "aging_job_id": None,
        "error": error,
        "summary": text,
        "correlation": correlation or {},
    }


def batch(items: list[dict[str, Any]], **patch: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "batch-1",
        "country": "GB",
        "kind": "scheduled",
        "status": "COMPLETED",
        "phase": "COMPLETED",
        "running": False,
        "channels_listed": 200,
        "channels_scanned": 200,
        "channels_above": len(items),
        "channels_analysed": sum(1 for i in items if i["status"] == "ANALYSED"),
        "channels_failed": sum(1 for i in items if i["status"] == "FAILED"),
        "window_from": WINDOW_FROM.isoformat(),
        "window_to": WINDOW_TO.isoformat(),
        "created_at": WINDOW_TO.isoformat(),
        "started_at": WINDOW_TO.isoformat(),
        "finished_at": WINDOW_TO.isoformat(),
        "error": None,
        "settings": DEFAULTS.snapshot(Thresholds()),
        "items": items,
    }
    return {**base, **patch}


# -- the settings ------------------------------------------------------------


def test_the_documented_defaults_are_the_defaults() -> None:
    assert DEFAULTS.analysis_duration_minutes == 2
    assert DEFAULTS.analysis_concurrency == 4
    assert DEFAULTS.max_runtime_minutes == 240
    assert DEFAULTS.aging_enabled is True
    assert DEFAULTS.aging_duration_days == 7
    assert DEFAULTS.aging_max_concurrent == 5
    assert DEFAULTS.correlation_tolerance_s == 120
    # Nothing is deleted unless an operator asks for it.
    assert DEFAULTS.retention_per_country == 0


def test_a_setting_outside_its_range_is_refused_rather_than_clamped() -> None:
    with pytest.raises(ValueError):
        BatchSettings(analysis_duration_minutes=0)
    with pytest.raises(ValueError):
        BatchSettings(analysis_concurrency=99)


def test_the_estimate_is_channels_divided_by_parallelism_times_duration() -> None:
    settings = BatchSettings(analysis_duration_minutes=2, analysis_concurrency=4)

    # Four at a time, two minutes each: thirty-seven channels is ten waves.
    assert settings.estimated_runtime_minutes(37) == 20
    assert settings.estimated_runtime_minutes(4) == 2
    assert settings.estimated_runtime_minutes(0) == 0


def test_the_snapshot_carries_the_threshold_and_window_the_batch_will_use() -> None:
    """
    The report has to state what its averages were judged against.

    Both can be edited between the run and the reading, so they are frozen with the batch.
    """
    snapshot = BatchSettings().snapshot(Thresholds())

    assert snapshot["threshold_pct"] == 0.25
    assert snapshot["window_days"] == 7
    assert BatchSettings.from_snapshot(snapshot).analysis_duration_minutes == 2


def test_a_snapshot_from_an_older_build_still_reads_back() -> None:
    """A field added later must not stop an old batch from being resumed or reported."""
    settings = BatchSettings.from_snapshot({"analysis_duration_minutes": 5, "unknown_field": 1})
    assert settings.analysis_duration_minutes == 5
    assert settings.analysis_concurrency == DEFAULTS.analysis_concurrency


def test_the_settings_endpoints_read_and_write(api: TestClient) -> None:
    body = api.get("/api/batch/settings").json()
    assert body["settings"]["analysis_duration_minutes"] == 2
    assert body["defaults"]["analysis_duration_minutes"] == 2
    assert body["effective"]["threshold_pct"] == 0.25

    saved = api.put(
        "/api/batch/settings",
        json={**body["settings"], "analysis_duration_minutes": 5},
    )
    assert saved.status_code == 200
    assert saved.json()["settings"]["analysis_duration_minutes"] == 5

    # Put it back, so the rest of the suite sees the documented default.
    api.put("/api/batch/settings", json=body["settings"])


def test_a_setting_the_api_refuses_does_not_change_what_is_stored(api: TestClient) -> None:
    before = api.get("/api/batch/settings").json()["settings"]
    assert (
        api.put("/api/batch/settings", json={**before, "analysis_concurrency": 0}).status_code
        == 422
    )
    assert api.get("/api/batch/settings").json()["settings"] == before


# -- the report --------------------------------------------------------------


def test_the_columns_are_in_the_order_the_specification_fixes() -> None:
    assert exports.headings(7) == (
        "Channel Name",
        "Service ID",
        "Country",
        "Avg Rebuffering Ratio (7 days)",
        "Week [start date - end date]",
        "Issues",
        "Analysis Summary",
    )


@pytest.mark.parametrize(
    ("days", "label"), [(7, "7 days"), (10, "10 days"), (1, "1 day"), (7.43, "7.4 days")]
)
def test_the_fourth_heading_states_the_window_that_was_measured(days: float, label: str) -> None:
    """
    The heading is written from the batch, never from a constant.

    A report that says "7 days" over figures covering ten would be wrong in a way nobody
    could see from the file.
    """
    assert exports.headings(days)[3] == f"Avg Rebuffering Ratio ({label})"


def test_the_report_opens_with_its_header_row_then_one_row_per_channel() -> None:
    rows = exports.csv_bytes(
        batch([item(service_id="GB1", name="Worst", average=2.4, text="a summary")])
    ).decode()
    lines = rows.splitlines()

    # The fourth heading names the source and the dates, so no average is read without them.
    assert next(csv.reader([lines[0]])) == [
        *exports.headings(7)[:3],
        "Avg Rebuffering Ratio (realtime, 2026-09-11 → 2026-09-18)",
        *exports.headings(7)[4:],
    ]
    assert lines[1].startswith("Worst,GB1,GB,2.4000,")
    assert "2026-09-11 - 2026-09-18 UTC" in lines[1]
    assert lines[1].endswith("a summary")


def test_rows_are_sorted_by_average_worst_first() -> None:
    rows = exports.csv_bytes(
        batch(
            [
                item(service_id="GB2", name="Second", average=0.9),
                item(service_id="GB1", name="Worst", average=2.4),
                item(service_id="GB3", name="Third", average=0.3),
            ]
        )
    ).decode()
    names = [line.split(",")[0] for line in rows.splitlines()[1:4]]

    assert names == ["Worst", "Second", "Third"]


def test_a_failed_channel_is_listed_with_its_reason_rather_than_dropped() -> None:
    """A report that quietly omits six channels is worse than one that names them."""
    text = exports.csv_bytes(
        batch(
            [
                item(service_id="GB1", name="Analysed", average=2.4),
                item(
                    service_id="GB2",
                    name="Broken",
                    average=1.1,
                    status="FAILED",
                    error="the playlist did not parse",
                ),
            ]
        )
    ).decode()

    # It is not in the main table...
    assert text.splitlines()[1].startswith("Analysed,")
    assert "Broken" not in text.splitlines()[1]
    # ...but it is in the file, with why.
    assert "Channels selected but not analysed" in text
    assert "the playlist did not parse" in text


def test_the_context_states_the_window_threshold_and_counts() -> None:
    text = exports.csv_bytes(batch([item(service_id="GB1", name="Worst", average=2.4)])).decode()

    assert "batch_id,batch-1" in text
    assert "batch_type,scheduled" in text
    assert "threshold_pct,0.25" in text
    assert "window_days,7 days" in text
    assert "channels_scanned,200" in text


def test_the_correlation_section_lists_every_spike_window() -> None:
    correlation = {
        "windows": [
            {
                "start": "2026-09-14T03:00:00+00:00",
                "end": "2026-09-14T03:02:00+00:00",
                "peak_pct": 4.0,
                "minutes": 3,
                "event_count": 2,
                "sentence": "Spike at 2026-09-14 03:00-03:02 UTC: 2 aging event(s) in window.",
                "matched": True,
            }
        ]
    }
    text = exports.csv_bytes(
        batch([item(service_id="GB1", name="Worst", average=2.4, correlation=correlation)])
    ).decode()

    assert "Rebuffering spike windows" in text
    assert ",".join(exports.CORRELATION_COLUMNS) in text
    assert "2 aging event(s) in window" in text


def test_the_workbook_puts_each_section_on_its_own_sheet() -> None:
    from openpyxl import load_workbook

    data = exports.xlsx_bytes(
        batch(
            [
                item(service_id="GB1", name="Worst", average=2.4),
                item(service_id="GB2", name="Broken", average=1.1, status="FAILED", error="boom"),
            ]
        )
    )
    workbook = load_workbook(io.BytesIO(data))
    sheet = workbook[exports.REPORT_SHEET]

    assert exports.REPORT_SHEET in workbook.sheetnames
    assert exports.FAILED_SHEET in workbook.sheetnames
    assert exports.ABOUT_SHEET in workbook.sheetnames
    assert [str(cell.value) for cell in next(sheet.iter_rows(max_row=1))] == list(
        exports.headings(7, source="realtime", dates="2026-09-11 → 2026-09-18")
    )
    assert sheet.freeze_panes == "A2"


def test_the_filename_names_the_country_and_the_day() -> None:
    assert (
        exports.filename("gb", "xlsx", dt.datetime(2026, 9, 18, tzinfo=dt.UTC))
        == "automated_batch_GB_20260918.xlsx"
    )


# -- the issues and analysis summary cells --------------------------------------------


def _finding(rule_id: str, severity: str, title: str, **extra: Any) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "title": title,
        "root_cause": extra.pop("root_cause", f"Root cause of {title.lower()}."),
        "detail": extra.pop("detail", f"Evidence for {title.lower()}."),
        "variant": extra.pop("variant", "v720p@1500k"),
        "count": extra.pop("count", 1),
        "first_seen": extra.pop("first_seen", "2026-11-09T00:00:00+00:00"),
        **extra,
    }


def test_a_channel_with_no_error_or_critical_issue_says_so() -> None:
    """An empty cell reads as an oversight; this reads as a result."""
    cells = summary.build(
        status="ANALYSED",
        findings=[_finding("SEG-099", "WARN", "A warning"), _finding("INF-001", "INFO", "x")],
    )
    assert cells.issues == summary.NO_ISSUES
    assert cells.analysis == summary.NOTHING_FOUND


def test_only_error_and_critical_issues_are_listed_one_per_line_worst_first() -> None:
    findings = [
        _finding("SEG-001", "ERROR", "Segment returned HTTP 404"),
        _finding("MED-020", "WARN", "A warning nobody acts on"),
        _finding("AUD-001", "INFO", "An informational note"),
        _finding("OK-001", "PASS", "A check that passed"),
        _finding(
            "SEG-010", "CRITICAL", "Presentation timestamps reset without a discontinuity tag"
        ),
    ]

    cells = summary.build(status="ANALYSED", findings=findings)

    assert cells.issues.splitlines() == [
        "Issue 1 - Presentation timestamps reset without a discontinuity tag",
        "Issue 2 - Segment returned HTTP 404",
    ]
    for hidden in ("warning", "informational", "passed", "WARN", "INFO", "PASS"):
        assert hidden not in cells.issues + cells.analysis


def test_a_rule_that_fired_on_several_renditions_is_one_issue() -> None:
    findings = [
        _finding(
            "SEG-010",
            "CRITICAL",
            "Timestamps reset",
            variant="v480p@750k",
            count=3,
            detail="Segment 86376 on v480p@750k starts at PTS 3880814632.",
        ),
        _finding(
            "SEG-010",
            "CRITICAL",
            "Timestamps reset",
            variant="v720p@1500k",
            count=1,
            detail="Segment 90001 on v720p@1500k starts at PTS 1.",
        ),
    ]

    cells = summary.build(status="ANALYSED", findings=findings)

    assert cells.issues == "Issue 1 - Timestamps reset"
    # The occurrence seen most is the one quoted, and the renditions are named.
    assert "Segment 86376 on v480p@750k" in cells.analysis
    assert "Seen on 2 renditions: v480p@750k, v720p@1500k." in cells.analysis


def test_the_analysis_summary_explains_each_issue_under_the_same_number() -> None:
    findings = [
        _finding(
            "SEG-010",
            "CRITICAL",
            "Presentation timestamps reset without a discontinuity tag",
            root_cause=(
                "The timestamp base changed with no discontinuity declared, so the player maps "
                "the new media onto a position it has already played."
            ),
            detail="Segment 86376 on v480p@750k starts at PTS 3880814632 (see SEG-010)",
        ),
        _finding("SEQ-001", "ERROR", "Media sequence number advanced by a different amount"),
    ]

    cells = summary.build(status="ANALYSED", findings=findings)
    paragraphs = cells.analysis.split("\n\n")

    assert paragraphs[0] == (
        "Issue 1 - The timestamp base changed with no discontinuity declared, so the player "
        "maps the new media onto a position it has already played. Segment 86376 on "
        "v480p@750k starts at PTS 3880814632."
    )
    assert paragraphs[1].startswith("Issue 2 - Root cause of media sequence number")


def test_no_rule_identifier_reaches_either_cell() -> None:
    findings = [
        _finding(
            "SEG-010",
            "ERROR",
            "Timestamps reset (SEG-010)",
            root_cause="As INFO-005 records, a boundary was skipped.",
        )
    ]

    cells = summary.build(status="ANALYSED", findings=findings)

    assert not re.search(r"[A-Z]{2,5}-\d{3}", cells.issues + cells.analysis)


def test_the_excluded_rules_are_left_out_of_the_report() -> None:
    """A stale playlist, a rung leaving LIVE and a re-signed URI describe the live window, not
    a packaging defect, so the partner report leaves them out by default."""
    findings = [
        _finding("MED-004", "ERROR", "Live playlist is stale"),
        _finding("MED-014", "ERROR", "Media playlist left the LIVE state"),
        _finding("MED-016", "ERROR", "Segment URI changed for an unchanged media sequence number"),
        _finding("SEG-001", "ERROR", "Segment returned HTTP 404"),
    ]

    cells = summary.build(
        status="ANALYSED", findings=findings, excluded_rules=DEFAULTS.report_excluded_rules
    )

    assert cells.issues == "Issue 1 - Segment returned HTTP 404"
    assert "stale" not in cells.analysis.lower()
    # Only what the setting names is left out: emptied, every issue is back.
    assert len(summary.build(status="ANALYSED", findings=findings).issues.splitlines()) == 4


def test_the_cells_say_nothing_about_spikes_or_aging() -> None:
    """The correlation stays on the item and in its own sheet; the partner never sees it."""
    cells = summary.build(
        status="ANALYSED", findings=[_finding("SEG-001", "ERROR", "Segment returned HTTP 404")]
    )
    for internal in ("spike", "aging", "rebuffering", "correlat", "finding(s)", "incident"):
        assert internal not in (cells.issues + cells.analysis).lower()


def test_the_excluded_rules_setting_is_normalised() -> None:
    settings = BatchSettings(report_excluded_rules=[" med-004", "MED-004", "seg-010 ", ""])
    assert settings.report_excluded_rules == ["MED-004", "SEG-010"]
    assert DEFAULTS.report_excluded_rules == ["MED-004", "MED-014", "MED-016"]


def test_the_report_puts_issues_and_their_summary_in_their_own_columns() -> None:
    row = item(service_id="GB1", name="Worst", average=2.4, text="Issue 1 - Why it happens.")
    row["issues"] = "Issue 1 - Segment returned HTTP 404\nIssue 2 - Timestamps reset"

    lines = list(csv.reader(io.StringIO(exports.csv_bytes(batch([row])).decode())))

    assert lines[0][-2:] == ["Issues", "Analysis Summary"]
    assert lines[1][-2] == "Issue 1 - Segment returned HTTP 404\nIssue 2 - Timestamps reset"
    assert lines[1][-1] == "Issue 1 - Why it happens."


def test_the_workbook_wraps_the_text_columns_so_each_issue_reads_on_its_own_line() -> None:
    from openpyxl import load_workbook

    row = item(service_id="GB1", name="Worst", average=2.4, text="Issue 1 - Why.")
    row["issues"] = "Issue 1 - A\nIssue 2 - B"
    book = load_workbook(io.BytesIO(exports.xlsx_bytes(batch([row]))))
    sheet = book[exports.REPORT_SHEET]
    header = [cell.value for cell in sheet[1]]
    issues_cell = sheet.cell(row=2, column=header.index("Issues") + 1)

    assert issues_cell.value == "Issue 1 - A\nIssue 2 - B"
    assert issues_cell.alignment.wrap_text is True


def test_a_failed_channel_says_what_failed() -> None:
    cells = summary.build(status="FAILED", error="the playlist did not parse")
    assert cells.analysis == "The analysis failed: the playlist did not parse."
    assert cells.issues == ""


def test_a_channel_with_no_playback_url_says_why_it_could_not_be_analysed() -> None:
    assert summary.build(status="NO_URL").analysis == summary.NO_URL


def test_a_channel_the_batch_did_not_reach_says_so() -> None:
    cells = summary.build(status="SKIPPED", error="the batch reached its maximum runtime")
    assert "selected but not reached" in cells.analysis


# -- selection ---------------------------------------------------------------


def test_a_channel_qualifies_on_its_average_not_on_one_bad_minute() -> None:
    """
    The rule the whole batch turns on, asserted at the level the batch uses it.

    A week at 0.05 % with a single 4 % minute averages far below the threshold. Selecting it
    would put a clean channel through a two-minute analysis and into the report.
    """
    from app.cascada.series import is_above

    thresholds = Thresholds()
    spiky = Stats(
        average_pct=0.0095,
        max_pct=4.0,
        max_at=None,
        minutes_above=1,
        minutes_counted=420,
        minutes_missing=0,
        percent_time_above=0.24,
        previous_week_average_pct=0.01,
        threshold_pct=thresholds.cascada_rebuffering_threshold_pct,
        above_threshold=False,
    )
    sustained = Stats(
        average_pct=0.40,
        max_pct=1.2,
        max_at=None,
        minutes_above=300,
        minutes_counted=420,
        minutes_missing=0,
        percent_time_above=71.0,
        previous_week_average_pct=0.2,
        threshold_pct=thresholds.cascada_rebuffering_threshold_pct,
        above_threshold=True,
    )

    assert is_above(spiky.average_pct, thresholds) is False
    assert is_above(sustained.average_pct, thresholds) is True


# -- the endpoints -----------------------------------------------------------


def test_the_report_columns_endpoint_states_what_a_download_contains(api: TestClient) -> None:
    body = api.get("/api/batch/columns").json()

    assert body["columns"][0] == "Channel Name"
    assert body["columns"][-1] == "Analysis Summary"
    assert body["window_label"] == "7 days"


def test_reading_a_batch_that_does_not_exist_is_a_404(api: TestClient) -> None:
    assert api.get("/api/batch/batches/nope").status_code == 404
    assert api.delete("/api/batch/batches/nope").status_code == 404
    assert api.get("/api/batch/batches/nope/log").status_code == 404
    assert api.get("/api/batch/batches/nope/report.csv").status_code == 404


def test_an_unsupported_report_format_is_refused(api: TestClient) -> None:
    assert api.get("/api/batch/batches/any/report.pdf").status_code == 400


async def test_a_second_batch_for_a_country_is_refused_with_the_first_ones_id(
    api: TestClient,
) -> None:
    """Two batches for one country would scan the same channels and analyse them twice."""
    await store.create(batch_id="already-running", country="IN", kind="manual", snapshot={})

    response = api.post("/api/batch/batches", json={"country": "IN"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["batch_id"] == "already-running"
    assert "already" in detail["message"]

    await store.update("already-running", status=store.CANCELLED)


def test_a_batch_for_a_country_nobody_serves_is_refused(api: TestClient) -> None:
    assert api.post("/api/batch/batches", json={"country": "ZZ"}).status_code == 400


def test_the_health_endpoint_says_whether_a_batch_could_run(api: TestClient) -> None:
    body = api.get("/api/batch/health").json()

    assert "cascada_session" in body
    assert isinstance(body["ready"], bool)
    if not body["ready"]:
        assert "CASCADA session" in body["reason"]


# -- the stored samples the aging charts read --------------------------------


def test_thinning_keeps_the_first_and_the_last_point() -> None:
    """
    A week of samples is too much to send, and the ends are what a range filter is judged by.

    Even thinning rather than clever thinning: an algorithm that kept "interesting" points
    would drop the flat stretches that show a stream behaving.
    """
    from app.api.job_samples import thin

    rows = list(range(1000))
    picked = thin(rows, 100)

    assert len(picked) == 100
    assert picked[0] == 0
    assert picked[-1] == 999
    assert picked == sorted(picked), "thinning preserves order"


def test_a_series_shorter_than_the_limit_is_returned_whole() -> None:
    from app.api.job_samples import thin

    rows = list(range(10))
    assert thin(rows, 100) is rows


def test_the_samples_endpoint_refuses_a_timestamp_it_cannot_read(api: TestClient) -> None:
    """A range the caller got wrong is said so, not silently ignored."""
    response = api.get("/api/aging/jobs/any/samples", params={"from": "last tuesday"})
    assert response.status_code in (400, 404)


def test_the_samples_endpoint_is_a_404_for_a_job_that_never_existed(api: TestClient) -> None:
    assert api.get("/api/aging/jobs/nope/samples").status_code == 404


def test_a_rung_declares_its_bitrate_in_its_own_identifier() -> None:
    """
    The ladder table is not among the stored samples, so the bitrate chart would have nothing
    to compare a measurement against. The rung identifier is generated from the master
    playlist — `v1080p@5000k` is the rung declaring `BANDWIDTH=5000000` — so the declared rate
    is recovered exactly rather than guessed.
    """
    from app.api.job_samples import declared_bandwidth

    rates = declared_bandwidth(["v1080p@5000k", "v720p@3000k", "v1080p@5000k"])

    assert rates == {"v1080p@5000k": 5_000_000, "v720p@3000k": 3_000_000}


def test_a_rung_that_declares_no_rate_carries_null_rather_than_zero() -> None:
    """
    A media-only playlist has no master to declare a bitrate. Zero would draw a bar claiming
    the channel declares nothing, which is a measurement nobody took; null draws no bar.
    """
    from app.api.job_samples import declared_bandwidth

    rates = declared_bandwidth(["media", "v0@0k"])

    assert rates == {"media": None, "v0@0k": None}

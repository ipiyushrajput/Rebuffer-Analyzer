"""The scan phase of a batch, and the one property a stuck-looking run depends on.

A country scan is hundreds of CASCADA calls and takes minutes. The only thing that tells an
operator the difference between a scan that is working and one that has hung is a counter that
moves, so that is what is asserted here: the batch row's `channels_scanned` rises **while the
scan is still running**, not when it finishes.

The batch drives `cascada.start_scan` rather than walking the channels itself, so these tests
hold a `Scan` object still and step it by hand. That is also the second property asserted: the
batch and the CASCADA Data tab run the same scan, so they cannot disagree about which channels
are rebuffering.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.batch import runner, store
from app.batch.settings import DEFAULTS
from app.cascada import service as cascada
from app.cascada.series import Stats, Window
from app.cascada.store import ChannelWindow
from app.db import session as db_session
from app.db.models import Batch, BatchItem, BatchLog
from app.main import create_app


async def _clear() -> None:
    """Every batch row, gone.

    Starting the application runs `sweep_unfinished`, which resumes every batch the database
    still shows as running. Rows another test left behind would therefore be picked up here
    and re-run against the real CASCADA, writing into the tables these tests are asserting on.
    """
    from sqlalchemy import delete

    async with db_session.session_scope() as session:
        await session.execute(delete(BatchLog))
        await session.execute(delete(BatchItem))
        await session.execute(delete(Batch))


@pytest.fixture(autouse=True)
def api() -> TestClient:
    """Starting the app creates the schema these tests write to."""
    with TestClient(create_app()) as client:
        # The sweep has already run by now, so anything it started is cancelled and the
        # tables are emptied before the test writes its own rows.
        for task in list(runner._tasks.values()):
            task.cancel()
        runner._tasks.clear()
        runner._cancelled.clear()
        client.portal.call(_clear)  # type: ignore[attr-defined]
        yield client
        client.portal.call(_clear)  # type: ignore[attr-defined]


WINDOW = Window(
    start=dt.datetime(2026, 9, 11, tzinfo=dt.UTC),
    end=dt.datetime(2026, 9, 18, tzinfo=dt.UTC),
)


def stats(average: float, *, above: bool) -> Stats:
    return Stats(
        average_pct=average,
        max_pct=average * 3,
        max_at=WINDOW.start,
        minutes_above=12,
        minutes_counted=10_080,
        minutes_missing=0,
        percent_time_above=0.1,
        previous_week_average_pct=average / 2,
        threshold_pct=0.25,
        above_threshold=above,
    )


def window(service_id: str, name: str, average: float, *, above: bool) -> ChannelWindow:
    return ChannelWindow(
        service_id=service_id,
        channel_name=name,
        country="GB",
        window=WINDOW,
        stats=stats(average, above=above),
        origin=[],
        comparison=[],
        fetched_at=dt.datetime.now(dt.UTC),
        truncated=False,
    )


def held_scan(total: int) -> cascada.Scan:
    """A scan that has started and will not move until a test moves it."""
    return cascada.Scan(
        id="held",
        country="GB",
        window=WINDOW,
        total=total,
        threshold_pct=0.25,
        urls={f"s{index}": f"https://cdn.example/{index}/index.m3u8" for index in range(total)},
    )


async def make_batch() -> str:
    batch_id = f"scan-{dt.datetime.now(dt.UTC).timestamp()}"
    await store.create(
        batch_id=batch_id, country="GB", kind=store.MANUAL, snapshot=DEFAULTS.snapshot()
    )
    return batch_id


@pytest.mark.asyncio()
async def test_the_scan_counter_moves_while_the_scan_is_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The defect this exists to prevent.

    A scan that writes its progress only after the last channel returns leaves a large country
    reporting `channels_scanned: 0` for many minutes, which reads exactly like a hang. The
    counter has to move while the scan is in flight.
    """
    monkeypatch.setattr(runner, "SCAN_POLL_S", 0.01)
    scan = held_scan(200)

    async def fake_start(country: str, concurrency: int | None = None) -> cascada.Scan:
        return scan

    monkeypatch.setattr(cascada, "start_scan", fake_start)

    batch_id = await make_batch()
    task = asyncio.create_task(runner._scan(batch_id, "GB", DEFAULTS))
    try:
        # The scan reports 40 channels measured and is still going.
        scan.done = 40
        for _ in range(100):
            await asyncio.sleep(0.01)
            if (await store.read(batch_id) or {})["channels_scanned"] == 40:
                break

        row = await store.read(batch_id) or {}
        assert row["channels_scanned"] == 40, "progress is written before the scan finishes"
        assert row["channels_listed"] == 200
        assert scan.finished_at is None, "the scan really is still running"
    finally:
        scan.status = "COMPLETED"
        scan.done = 200
        scan.finished_at = dt.datetime.now(dt.UTC)
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio()
async def test_the_scan_selects_what_the_cascada_scan_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The batch and the CASCADA Data tab read the same selection, worst average first.

    `scan.above` is the module's own rule — the average alone, never one bad minute — so a
    second rule here could only ever disagree with the tab.
    """
    monkeypatch.setattr(runner, "SCAN_POLL_S", 0.01)
    scan = held_scan(3)
    scan.results = {
        "s0": window("s0", "Quiet channel", 0.05, above=False),
        "s1": window("s1", "Bad channel", 4.10, above=True),
        "s2": window("s2", "Worse channel", 9.90, above=True),
    }
    scan.done = 3
    scan.status = "COMPLETED"
    scan.finished_at = dt.datetime.now(dt.UTC)

    async def fake_start(country: str, concurrency: int | None = None) -> cascada.Scan:
        return scan

    monkeypatch.setattr(cascada, "start_scan", fake_start)

    batch_id = await make_batch()
    await runner._scan(batch_id, "GB", DEFAULTS)

    items = await store.items_for(batch_id)
    assert [item["service_id"] for item in items] == ["s2", "s1"], "worst average first"
    assert [item["status"] for item in items] == ["PENDING", "PENDING"]
    assert items[0]["average_pct"] == 9.90

    row = await store.read(batch_id) or {}
    assert (row["channels_scanned"], row["channels_above"]) == (3, 2)
    assert row["channels_failed"] == 0


@pytest.mark.asyncio()
async def test_an_above_threshold_channel_with_no_playback_url_is_kept_and_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalogue lists channels with no `CNTN_URI`. Dropping one would hide a defect."""
    monkeypatch.setattr(runner, "SCAN_POLL_S", 0.01)
    scan = held_scan(1)
    scan.urls = {"s0": ""}
    scan.results = {"s0": window("s0", "No URL channel", 3.0, above=True)}
    scan.done = 1
    scan.status = "COMPLETED"
    scan.finished_at = dt.datetime.now(dt.UTC)

    async def fake_start(country: str, concurrency: int | None = None) -> cascada.Scan:
        return scan

    monkeypatch.setattr(cascada, "start_scan", fake_start)

    batch_id = await make_batch()
    await runner._scan(batch_id, "GB", DEFAULTS)

    items = await store.items_for(batch_id)
    assert [item["status"] for item in items] == ["NO_URL"]

    lines = " ".join(line["message"] for line in await store.log_lines(batch_id))
    assert "carry no playback URL" in lines


@pytest.mark.asyncio()
async def test_a_session_rejected_mid_scan_fails_the_batch_with_that_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every remaining channel would be rejected the same way, so the batch stops and says so."""
    monkeypatch.setattr(runner, "SCAN_POLL_S", 0.01)
    scan = held_scan(10)
    scan.done = 2
    scan.status = "AUTH_FAILED"
    scan.error = "CASCADA rejected the session. Paste a fresh session in the Settings tab."
    scan.finished_at = dt.datetime.now(dt.UTC)

    async def fake_start(country: str, concurrency: int | None = None) -> cascada.Scan:
        return scan

    monkeypatch.setattr(cascada, "start_scan", fake_start)

    batch_id = await make_batch()
    with pytest.raises(cascada.CascadaAuthError) as raised:
        await runner._scan(batch_id, "GB", DEFAULTS)

    assert "Paste a fresh session" in str(raised.value)
    # What it measured before the session died is still written down.
    assert (await store.read(batch_id) or {})["channels_scanned"] == 2


@pytest.mark.asyncio()
async def test_a_failed_channel_is_named_in_the_log_rather_than_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "SCAN_POLL_S", 0.01)
    scan = held_scan(2)
    scan.results = {"s0": window("s0", "Bad channel", 5.0, above=True)}
    scan.failures = [
        cascada.Failure(service_id="s1", channel_name="Silent channel", reason="HTTP 504")
    ]
    scan.done = 2
    scan.status = "COMPLETED"
    scan.finished_at = dt.datetime.now(dt.UTC)

    async def fake_start(country: str, concurrency: int | None = None) -> cascada.Scan:
        return scan

    monkeypatch.setattr(cascada, "start_scan", fake_start)

    batch_id = await make_batch()
    await runner._scan(batch_id, "GB", DEFAULTS)

    assert (await store.read(batch_id) or {})["channels_failed"] == 1
    lines = " ".join(line["message"] for line in await store.log_lines(batch_id))
    assert "s1 Silent channel" in lines and "HTTP 504" in lines


@pytest.mark.asyncio()
async def test_cancelling_the_batch_stops_the_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled batch must not leave a country scan running behind it."""
    monkeypatch.setattr(runner, "SCAN_POLL_S", 0.01)
    scan = held_scan(100)
    stopped: list[str] = []

    async def fake_start(country: str, concurrency: int | None = None) -> cascada.Scan:
        return scan

    async def fake_cancel(scan_id: str) -> cascada.Scan:
        stopped.append(scan_id)
        scan.status = "CANCELLED"
        scan.finished_at = dt.datetime.now(dt.UTC)
        return scan

    monkeypatch.setattr(cascada, "start_scan", fake_start)
    monkeypatch.setattr(cascada, "cancel_scan", fake_cancel)

    batch_id = await make_batch()
    task = asyncio.create_task(runner._scan(batch_id, "GB", DEFAULTS))
    await asyncio.sleep(0.05)
    runner._cancelled.add(batch_id)
    try:
        await asyncio.wait_for(task, timeout=5)
    finally:
        runner._cancelled.discard(batch_id)

    assert stopped == ["held"], "the CASCADA scan was told to stop"

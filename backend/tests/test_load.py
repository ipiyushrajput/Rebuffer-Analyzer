"""Load behaviour under the concurrency the deployment is sized for.

Twenty concurrent aging jobs is the configured ceiling (`RBA_MAX_CONCURRENT_JOBS`), so this
is the shape the analyzer must hold: every job admitted, every job answering its status
endpoint, memory bounded, and a clean shutdown that leaves every job reportable.
"""

from __future__ import annotations

import gc
import time

from fastapi.testclient import TestClient

from app.main import create_app
from tests.fixtures.server import FixtureServer, build_simple_channel

CONCURRENT_JOBS = 20


def _rss_mb() -> float | None:
    """Resident set size in MB, read from /proc when it is available."""
    try:
        with open("/proc/self/statm") as handle:
            pages = int(handle.read().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return pages * 4096 / 1024 / 1024


def test_twenty_concurrent_aging_jobs_all_run_and_shut_down_cleanly(
    origin: FixtureServer,
) -> None:
    url = build_simple_channel(origin, variant_count=2, segment_count=4)

    gc.collect()
    before = _rss_mb()

    with TestClient(create_app()) as client:
        job_ids: list[str] = []
        started = time.time()
        for index in range(CONCURRENT_JOBS):
            response = client.post(
                "/api/aging/jobs",
                json={
                    "playback_url": url,
                    "channel_name": f"Load channel {index:02d}",
                    "duration_minutes": 5,
                },
            )
            assert response.status_code == 201, response.text
            job_ids.append(response.json()["id"])

        assert len(set(job_ids)) == CONCURRENT_JOBS
        submit_seconds = time.time() - started
        assert submit_seconds < 30, f"submitting {CONCURRENT_JOBS} jobs took {submit_seconds:.1f} s"

        # Every job must be admitted by the semaphore, not queued behind the others.
        deadline = time.time() + 60
        running: set[str] = set()
        while time.time() < deadline and len(running) < CONCURRENT_JOBS:
            listing = client.get("/api/aging/jobs").json()["jobs"]
            running = {job["id"] for job in listing if job["status"] == "RUNNING"}
            time.sleep(0.5)
        assert len(running) == CONCURRENT_JOBS, f"only {len(running)} of {CONCURRENT_JOBS} started"

        # Every job answers its own status endpoint while the others are running.
        for job_id in job_ids:
            body = client.get(f"/api/aging/jobs/{job_id}").json()
            assert body["status"] == "RUNNING"
            assert body["remaining_s"] > 0

        after = _rss_mb()
        if before is not None and after is not None:
            growth = after - before
            # Sampling is bounded per job, so twenty jobs stay well inside the container cap.
            assert growth < 1500, (
                f"resident set grew by {growth:.0f} MB across {CONCURRENT_JOBS} jobs"
            )

        # Cancelling every job leaves each one reportable.
        for job_id in job_ids:
            assert client.delete(f"/api/aging/jobs/{job_id}").status_code == 200

        for job_id in job_ids:
            body = client.get(f"/api/aging/jobs/{job_id}").json()
            assert body["status"] in ("CANCELLED", "COMPLETED", "CANCELLING")

    # Leaving the context runs the lifespan shutdown; it returns rather than hanging.
    gc.collect()


def test_the_job_manager_shuts_down_while_jobs_are_still_running(origin: FixtureServer) -> None:
    url = build_simple_channel(origin, variant_count=1, segment_count=3)

    started = time.time()
    with TestClient(create_app()) as client:
        for index in range(5):
            client.post(
                "/api/aging/jobs",
                json={
                    "playback_url": url,
                    "channel_name": f"Shutdown channel {index}",
                    "duration_minutes": 60,
                },
            )
        time.sleep(2.0)
    # Graceful shutdown stops every job rather than waiting out the 60-minute window.
    assert time.time() - started < 90

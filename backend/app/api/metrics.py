"""Prometheus metrics for this instance: `GET /api/metrics`.

Everything here is read from memory — no database or storage call — so a scrape every 15 s
costs nothing. Every series carries the instance id, because with several instances behind
one load balancer a scrape reaches one of them and the dashboard has to say which.
"""

from __future__ import annotations

import os
from collections import Counter

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.analysis import recorder as recorder_module
from app.config import get_settings
from app.jobs import lease
from app.jobs.manager import job_manager
from app.reports import artifacts

router = APIRouter(tags=["metrics"])


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _rss_bytes() -> int:
    """Resident memory, from /proc on Linux; 0 where it is not available."""
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def _pool() -> tuple[int, int]:
    from app.db import session as db_session

    try:
        pool = db_session.engine().pool
        checked_out = int(getattr(pool, "checkedout", lambda: 0)())
        size = int(getattr(pool, "size", lambda: 0)())
    except Exception:
        return 0, 0
    return checked_out, size


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    me = _label(lease.instance_id())
    lines: list[str] = []

    def gauge(name: str, help_text: str, samples: list[tuple[dict[str, str], float]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in samples:
            tags = ",".join(f'{k}="{_label(v)}"' for k, v in {"instance": me, **labels}.items())
            lines.append(f"{name}{{{tags}}} {value}")

    def counter(
        name: str, help_text: str, value: float, labels: dict[str, str] | None = None
    ) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} counter")
        tags = ",".join(f'{k}="{_label(v)}"' for k, v in {"instance": me, **(labels or {})}.items())
        lines.append(f"{name}{{{tags}}} {value}")

    handles = list(job_manager.jobs.values())
    by_state = Counter((h.type, h.status) for h in handles)
    gauge(
        "streamlens_jobs",
        "Jobs this instance holds, by type and status.",
        [({"type": t, "status": s}, float(n)) for (t, s), n in sorted(by_state.items())],
    )
    running = [h for h in handles if h.status in ("RUNNING", "PENDING")]
    gauge(
        "streamlens_jobs_capacity",
        "Jobs this instance runs at once (RBA_MAX_CONCURRENT_JOBS).",
        [({}, float(get_settings().rba_max_concurrent_jobs))],
    )
    lags = [
        h.session.self_check.lag_max_s * 1000
        for h in running
        if h.session is not None and h.session.self_check is not None
    ]
    gauge(
        "streamlens_event_loop_lag_max_ms",
        "Worst event-loop lag any running analysis measured (INFO-007 past the threshold).",
        [({}, round(max(lags), 1) if lags else 0.0)],
    )
    checked_out, size = _pool()
    gauge(
        "streamlens_db_pool_checked_out", "Database connections in use.", [({}, float(checked_out))]
    )
    gauge("streamlens_db_pool_size", "Database connections kept open.", [({}, float(size))])
    gauge(
        "streamlens_process_resident_bytes",
        "Resident memory of this process.",
        [({}, float(_rss_bytes()))],
    )
    gauge(
        "streamlens_recordings_local_bytes",
        "Bytes of recordings on this host, waiting for their run to end and move to storage.",
        [({}, float(recorder_module.USAGE.bytes))],
    )
    stats = artifacts.STATS
    counter(
        "streamlens_archived_runs_total",
        "Finished runs whose archives were written.",
        stats.archived,
    )
    counter(
        "streamlens_archive_failures_total", "Archives that failed to be written.", stats.failed
    )
    counter(
        "streamlens_archive_uploaded_bytes_total",
        "Bytes written to object storage.",
        stats.uploaded_bytes,
    )
    gauge(
        "streamlens_up",
        "This instance answers.",
        [({"storage": get_settings().rba_storage_backend}, 1.0)],
    )
    return "\n".join(lines) + "\n"

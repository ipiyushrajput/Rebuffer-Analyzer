"""Health endpoint.

Reports what the analyzer can and cannot do right now. When a dependency is absent the
response says so as a fact, and the analysis layer records the corresponding INFO finding
rather than reporting a check as clean that never ran.
"""

from __future__ import annotations

import shutil
from typing import Any

from fastapi import APIRouter

from app.config import get_settings, get_thresholds
from app.db import session as db_session
from app.drm import decrypt
from app.media import ffprobe

router = APIRouter(tags=["health"])

VERSION = "1.0.0"


def _playwright_status() -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return {"installed": False, "detail": "playwright is not installed; PDF export is off"}
    return {"installed": True, "detail": "PDF export renders the same HTML report"}


def _mp4decrypt_status() -> dict[str, Any]:
    """Bento4, which is how a `cbcs` track is decrypted and the only way it is.

    `ok` is always true: `cenc` — every TV Plus channel measured so far — is decrypted in
    process with no binary at all, so a host without Bento4 is a working deployment, not a
    degraded one. What it cannot do is read the payload of a `cbcs` channel, and each such
    rendition says so for itself through INFO-001 and the report's protection table.
    """
    path = decrypt.mp4decrypt_path()
    return {
        "ok": True,
        "installed": path is not None,
        "path": path,
        "detail": (
            "cbcs tracks are decrypted with Bento4"
            if path
            else "cenc is decrypted in process; cbcs tracks are reported as not decryptable"
        ),
    }


@router.get("/health")
async def health() -> dict[str, Any]:
    binaries = ffprobe.binaries()
    database = await db_session.healthcheck()
    settings = get_settings()

    checks = {
        "ffmpeg": {"installed": bool(binaries.ffmpeg), "path": binaries.ffmpeg},
        "ffprobe": {"installed": bool(binaries.ffprobe), "path": binaries.ffprobe},
        "playwright": _playwright_status(),
        "mp4decrypt": _mp4decrypt_status(),
        "database": database,
    }
    degraded = [name for name, value in checks.items() if not _is_ok(value)]

    return {
        "status": "ok" if not degraded else "degraded",
        "version": VERSION,
        "degraded": degraded,
        "checks": checks,
        "limits": {
            "max_concurrent_jobs": settings.rba_max_concurrent_jobs,
            "bulk_default_concurrency": settings.rba_bulk_default_concurrency,
            "per_host_connections": settings.rba_per_host_connections,
        },
        "thresholds": get_thresholds().model_dump(mode="json"),
    }


def _is_ok(value: dict[str, Any]) -> bool:
    if "ok" in value:
        return bool(value["ok"])
    return bool(value.get("installed"))


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready")
async def ready() -> dict[str, Any]:
    database = await db_session.healthcheck()
    return {
        "ready": bool(database["ok"]),
        "database": database,
        "ffprobe": bool(shutil.which("ffprobe")),
    }

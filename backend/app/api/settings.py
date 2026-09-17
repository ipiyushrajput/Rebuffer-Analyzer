"""Settings API.

Thresholds are stored in the database so an edit in the Settings tab survives a restart and
applies to every job started afterwards. Running jobs keep the thresholds they started with,
so a report always states the values its findings were measured against.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter

from app.config import Thresholds, get_settings, get_thresholds, set_thresholds
from app.db import session as db_session
from app.db.models import SettingRow

logger = logging.getLogger(__name__)
router = APIRouter(tags=["settings"])

THRESHOLDS_KEY = "thresholds"
PREFERENCES_KEY = "preferences"

DEFAULT_PREFERENCES: dict[str, Any] = {
    "ua_profile": "tizen5",
    "max_concurrent_jobs": 20,
    "bulk_default_concurrency": 5,
    "sample_retention_days": 30,
    "record_evidence_realtime": False,
    "record_evidence_aging": True,
}


async def load_thresholds_from_db() -> Thresholds:
    """Replace the in-process thresholds with the stored ones. Defaults stay on failure."""
    try:
        async with db_session.session_scope() as session:
            row = await session.get(SettingRow, THRESHOLDS_KEY)
            if row is not None:
                return set_thresholds(Thresholds(**row.value))
    except Exception as exc:
        logger.warning("stored thresholds could not be loaded: %s", db_session.describe_error(exc))
    return get_thresholds()


async def _load_preferences() -> dict[str, Any]:
    try:
        async with db_session.session_scope() as session:
            row = await session.get(SettingRow, PREFERENCES_KEY)
            if row is not None:
                return {**DEFAULT_PREFERENCES, **row.value}
    except Exception as exc:
        logger.warning("stored preferences could not be loaded: %s", db_session.describe_error(exc))
    settings = get_settings()
    return {
        **DEFAULT_PREFERENCES,
        "max_concurrent_jobs": settings.rba_max_concurrent_jobs,
        "bulk_default_concurrency": settings.rba_bulk_default_concurrency,
        "sample_retention_days": settings.rba_sample_retention_days,
    }


@router.get("/settings")
async def read_settings() -> dict[str, Any]:
    return {
        "thresholds": get_thresholds().model_dump(mode="json"),
        "defaults": Thresholds().model_dump(mode="json"),
        "preferences": await _load_preferences(),
        "ua_profiles": list(_ua_profiles()),
    }


def _ua_profiles() -> list[str]:
    from app.config import USER_AGENT_PROFILES

    return list(USER_AGENT_PROFILES)


@router.put("/settings")
async def write_settings(payload: dict[str, Any]) -> dict[str, Any]:
    thresholds = get_thresholds()
    if "thresholds" in payload:
        thresholds = set_thresholds(
            Thresholds(**{**thresholds.model_dump(), **payload["thresholds"]})
        )
        await _store(THRESHOLDS_KEY, thresholds.model_dump(mode="json"))

    preferences = await _load_preferences()
    if "preferences" in payload:
        preferences = {**preferences, **payload["preferences"]}
        await _store(PREFERENCES_KEY, preferences)

    return {
        "thresholds": thresholds.model_dump(mode="json"),
        "preferences": preferences,
    }


async def _store(key: str, value: dict[str, Any]) -> None:
    async with db_session.session_scope() as session:
        row = await session.get(SettingRow, key)
        if row is None:
            session.add(SettingRow(key=key, value=value, updated_at=dt.datetime.now(dt.UTC)))
        else:
            row.value = value
            row.updated_at = dt.datetime.now(dt.UTC)


@router.get("/settings/rules")
async def rule_catalogue() -> dict[str, Any]:
    """The full rule catalogue, so the UI can explain any finding it renders."""
    from app.analysis.rules import catalogue  # noqa: F401 — declares the rules.
    from app.analysis.rules.base import registry

    return {
        "count": len(registry),
        "rules": [
            {
                "id": rule.id,
                "layer": rule.layer,
                "severity": rule.severity.value,
                "owner": rule.owner.value,
                "owner_label": rule.owner.label,
                "title": rule.title,
                "root_cause": rule.root_cause,
                "fix": rule.fix,
                "rebuffer_impact": rule.rebuffer_impact.value,
                "reference": rule.reference,
                "thresholds": list(rule.thresholds),
            }
            for rule in registry.all()
        ],
    }


@router.get("/settings/db-status")
async def db_status() -> dict[str, Any]:
    """Database reachability without the host, user, or credentials."""
    status = await db_session.healthcheck()
    stored = await _load_preferences()
    return {**status, "retention_days": stored["sample_retention_days"]}

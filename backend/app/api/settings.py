"""Settings API.

Thresholds are stored in the database so an edit in the Settings tab survives a restart and
applies to every job started afterwards. Running jobs keep the thresholds they started with,
so a report always states the values its findings were measured against.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import (
    USER_AGENT_PROFILE_TABLE,
    Thresholds,
    default_ua_profile,
    get_settings,
    get_thresholds,
    set_default_ua_profile,
    set_thresholds,
)
from app.db import session as db_session
from app.db.models import SettingRow

logger = logging.getLogger(__name__)
router = APIRouter(tags=["settings"])

THRESHOLDS_KEY = "thresholds"
PREFERENCES_KEY = "preferences"
RULE_SEVERITY_KEY = "rule_severity"

DEFAULT_PREFERENCES: dict[str, Any] = {
    "ua_profile": default_ua_profile(),
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


async def load_rule_severities_from_db() -> dict[str, str]:
    """Put the stored severity overrides back in force. An unreadable store changes nothing.

    A rule whose id is no longer declared, or a severity that is no longer a severity, is
    dropped rather than carried: the catalogue is the authority on what exists, and a stale
    row must not silently reclassify a rule that took its identifier later.
    """
    from app.analysis.rules import catalogue  # noqa: F401 — declares the rules.
    from app.analysis.rules.base import Severity, registry, set_severity_overrides

    stored: dict[str, Any] = {}
    try:
        async with db_session.session_scope() as session:
            row = await session.get(SettingRow, RULE_SEVERITY_KEY)
            if row is not None:
                stored = dict(row.value)
    except Exception as exc:
        logger.warning(
            "stored rule severities could not be loaded: %s", db_session.describe_error(exc)
        )
        return {}

    valid: dict[str, Severity] = {}
    for rule_id, value in stored.items():
        if rule_id in registry and value in Severity.__members__:
            valid[rule_id] = Severity[str(value)]
        else:
            logger.warning("stored severity override for %s was dropped: %s", rule_id, value)
    set_severity_overrides(valid)
    return {rule_id: severity.value for rule_id, severity in valid.items()}


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


def _ua_profiles() -> list[dict[str, str]]:
    """Every profile, with the string each one sends.

    The list is served rather than duplicated in the client: it used to exist twice, and the
    copy in `constants.ts` drifted from the one the requests actually go out with.
    """
    return [
        {"id": key, "label": profile.label, "user_agent": profile.user_agent}
        for key, profile in USER_AGENT_PROFILE_TABLE.items()
    ]


async def load_preferences_into_config() -> None:
    """Put the stored User-Agent default in force for jobs started from now on."""
    preferences = await _load_preferences()
    set_default_ua_profile(str(preferences.get("ua_profile") or default_ua_profile()))


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
        # In force immediately, not at the next restart: an operator who changes the profile
        # and starts a run expects that run to go out as what they chose.
        set_default_ua_profile(str(preferences.get("ua_profile") or default_ua_profile()))

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
    """The full rule catalogue, so the UI can explain any finding it renders.

    `severity` is what the rule reports today; `declared_severity` is what the catalogue
    declares. They differ exactly when an operator has reassigned it, and both are stated so
    a reader can see that a rule was reassigned rather than wondering why the catalogue and a
    report disagree.
    """
    from app.analysis.rules import catalogue  # noqa: F401 — declares the rules.
    from app.analysis.rules.base import Severity, registry, severity_overrides

    overrides = severity_overrides()
    return {
        "count": len(registry),
        "severities": [s.value for s in Severity],
        "overridden_count": len(overrides),
        "rules": [
            {
                "id": rule.id,
                "layer": rule.layer,
                "severity": overrides.get(rule.id, rule.severity).value,
                "declared_severity": rule.severity.value,
                "overridden": rule.id in overrides,
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


class RuleSeverityIn(BaseModel):
    """One reassignment. A null severity restores what the catalogue declares."""

    severity: str | None = None


@router.put("/settings/rules/{rule_id}")
async def write_rule_severity(rule_id: str, body: RuleSeverityIn) -> dict[str, Any]:
    """Reassign one rule's severity, or restore its declared one.

    The change applies to jobs started afterwards, like every other setting: a running job
    keeps what it started with, so a report always states the severities its findings were
    filed under.
    """
    from app.analysis.rules import catalogue  # noqa: F401 — declares the rules.
    from app.analysis.rules.base import Severity, registry, set_severity_overrides
    from app.analysis.rules.base import severity_overrides as current_overrides

    if rule_id not in registry:
        raise HTTPException(status_code=404, detail=f"{rule_id} is not a declared rule.")

    overrides = current_overrides()
    if body.severity is None:
        overrides.pop(rule_id, None)
    else:
        if body.severity not in Severity.__members__:
            raise HTTPException(
                status_code=400,
                detail=f"{body.severity} is not a severity. Use one of: "
                + ", ".join(s.value for s in Severity)
                + ".",
            )
        declared = registry.get(rule_id).severity
        chosen = Severity[body.severity]
        # Storing the declared severity is the same as storing nothing, and keeping the row
        # would mark the rule as reassigned when it is not.
        if chosen is declared:
            overrides.pop(rule_id, None)
        else:
            overrides[rule_id] = chosen

    set_severity_overrides(overrides)
    await _store(RULE_SEVERITY_KEY, {key: value.value for key, value in overrides.items()})
    return await rule_catalogue()


@router.get("/settings/db-status")
async def db_status() -> dict[str, Any]:
    """Database reachability without the host, user, or credentials."""
    status = await db_session.healthcheck()
    stored = await _load_preferences()
    return {**status, "retention_days": stored["sample_retention_days"]}

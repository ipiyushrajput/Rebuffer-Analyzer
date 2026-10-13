"""Alerts: a webhook the moment a run raises a finding worth waking someone for.

An aging run finds a CRITICAL defect at 03:00 and nobody knows until the report is read the
next morning. With a webhook set in Settings → Alerts, the first occurrence of each issue at or
above the chosen severity is posted while the run is still going — once per rule and layer per
run, so a defect that recurs every poll is one message, not thousands — and an issue
acknowledged on the channel is not posted at all.

The payload carries `text` (what Microsoft Teams and Slack incoming webhooks render) beside the
structured fields, so one URL works with either and with anything that reads JSON. The URL
holds a secret token: it is stored server-side and reported to a page as set or not set, never
returned. TLS verification is off on this request as on every outbound fetch (non-negotiable 1).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from app.analysis.review import AckMatch, is_acknowledged
from app.db import session as db_session
from app.db.models import SettingRow

logger = logging.getLogger(__name__)

SETTINGS_KEY = "alerts"
SEVERITY_RANK = {"CRITICAL": 4, "ERROR": 3, "WARN": 2, "INFO": 1, "PASS": 0}
TOOL_NAME = "StreamLens"


class AlertSettings(BaseModel):
    enabled: bool = False
    webhook_url: str = ""
    min_severity: str = Field(default="CRITICAL", pattern="^(CRITICAL|ERROR|WARN)$")
    job_types: list[str] = Field(default_factory=lambda: ["aging"])
    # Where a link in the message points: this deployment's public origin, when it has one.
    public_origin: str = ""

    def describe(self) -> dict[str, Any]:
        """What a page may know: everything but the webhook's token-bearing URL."""
        host = urlsplit(self.webhook_url).netloc if self.webhook_url else ""
        return {
            "enabled": self.enabled,
            "webhook_set": bool(self.webhook_url),
            "webhook_host": host,
            "min_severity": self.min_severity,
            "job_types": self.job_types,
            "public_origin": self.public_origin,
        }


async def load() -> AlertSettings:
    try:
        async with db_session.session_scope() as session:
            row = await session.get(SettingRow, SETTINGS_KEY)
        if row is not None and isinstance(row.value, dict):
            return AlertSettings(**row.value)
    except Exception as exc:
        logger.warning("alert settings could not be read: %s", type(exc).__name__)
    return AlertSettings()


async def save(settings: AlertSettings) -> None:
    async with db_session.session_scope() as session:
        row = await session.get(SettingRow, SETTINGS_KEY)
        value = settings.model_dump()
        if row is None:
            session.add(
                SettingRow(key=SETTINGS_KEY, value=value, updated_at=dt.datetime.now(dt.UTC))
            )
        else:
            row.value = value
            row.updated_at = dt.datetime.now(dt.UTC)


def message(
    *, channel: str, job_id: str, job_type: str, finding: dict[str, Any], origin: str
) -> dict[str, Any]:
    severity = str(finding.get("severity"))
    variant = finding.get("variant") or "every rendition"
    link = f"{origin.rstrip('/')}/#/channels/{job_id}" if origin else ""
    text = (
        f"[{TOOL_NAME}] {severity} on {channel} ({job_type}): "
        f"{finding.get('rule_id')} {finding.get('title')} — {variant}. "
        f"{finding.get('detail')} Owner: {finding.get('owner_label') or finding.get('owner')}."
        + (f" {link}" if link else "")
    )
    return {
        "text": text,
        "tool": TOOL_NAME,
        "channel": channel,
        "job_id": job_id,
        "job_type": job_type,
        "severity": severity,
        "rule_id": finding.get("rule_id"),
        "title": finding.get("title"),
        "variant": finding.get("variant"),
        "stream_layer": finding.get("stream_layer"),
        "owner": finding.get("owner"),
        "detail": finding.get("detail"),
        "fix": finding.get("fix"),
        "first_seen": finding.get("first_seen"),
        "link": link,
    }


async def post(url: str, payload: dict[str, Any]) -> tuple[bool, str]:
    """POST one alert. Never raises: an alert that fails is logged, the run carries on."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            response = await client.post(url, json=payload)
        if 200 <= response.status_code < 300:
            return True, ""
        return False, f"HTTP {response.status_code}"
    except Exception as exc:
        return False, type(exc).__name__


@dataclass
class JobAlerts:
    """One run's alerting: what it has already posted, and what its channel acknowledges."""

    job_id: str
    job_type: str
    channel: str
    settings: AlertSettings
    acknowledged: list[AckMatch] = field(default_factory=list)
    sent: set[tuple[str, str]] = field(default_factory=set)
    failures: int = 0

    @property
    def active(self) -> bool:
        return (
            self.settings.enabled
            and bool(self.settings.webhook_url)
            and self.job_type in self.settings.job_types
        )

    def wants(self, finding: dict[str, Any]) -> bool:
        if not self.active:
            return False
        if (
            SEVERITY_RANK.get(str(finding.get("severity")), 0)
            < SEVERITY_RANK[self.settings.min_severity]
        ):
            return False
        if is_acknowledged(self.acknowledged, str(finding.get("rule_id")), finding.get("variant")):
            return False
        return (str(finding.get("rule_id")), str(finding.get("stream_layer"))) not in self.sent

    async def observe(self, finding: dict[str, Any]) -> bool:
        """Post the finding if it is the first of its issue at or above the threshold."""
        if not self.wants(finding):
            return False
        self.sent.add((str(finding.get("rule_id")), str(finding.get("stream_layer"))))
        ok, problem = await post(
            self.settings.webhook_url,
            message(
                channel=self.channel,
                job_id=self.job_id,
                job_type=self.job_type,
                finding=finding,
                origin=self.settings.public_origin,
            ),
        )
        if not ok:
            self.failures += 1
            logger.warning("alert for job %s was not delivered: %s", self.job_id, problem)
        return ok

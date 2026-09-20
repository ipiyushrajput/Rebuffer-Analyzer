"""Reassigning a rule's severity.

A deployment does not always read a defect the way the catalogue declares it. A team that has
accepted one, or that treats one more seriously than the product does, reassigns it here
rather than editing the catalogue: the declaration stays the product's opinion and the
override is the deployment's, and both are reported so nobody has to guess which they are
looking at.

The override is about the rule, so it applies to every finding the rule raises — including
one whose detector computed a severity of its own.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.analysis.rules import catalogue as R
from app.analysis.rules.base import (
    Severity,
    StreamLayer,
    set_severity_overrides,
    severity_overrides,
)
from app.main import create_app


@pytest.fixture(autouse=True)
def clean_overrides() -> Iterator[None]:
    """The override map is process-wide, so it is emptied around every test."""
    set_severity_overrides({})
    yield
    set_severity_overrides({})


@pytest.fixture()
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        client.put("/api/settings/rules/HTTP-002", json={"severity": None})
        yield client
        client.put("/api/settings/rules/HTTP-002", json={"severity": None})


# -- what the catalogue declares ---------------------------------------------


def test_the_redirect_query_rule_is_a_warning() -> None:
    """A dropped query parameter is reported, not treated as a critical defect."""
    assert R.HTTP_QUERY_DROPPED.severity is Severity.WARN


# -- the override in force ---------------------------------------------------


def test_a_reassigned_rule_raises_findings_at_the_new_severity() -> None:
    set_severity_overrides({"HTTP-002": Severity.INFO})

    finding = R.HTTP_QUERY_DROPPED.raise_finding("The redirect dropped `token`.")

    assert finding.severity is Severity.INFO
    assert finding.rule.severity is Severity.WARN, "the declaration is unchanged"


def test_an_override_outranks_a_severity_the_detector_computed() -> None:
    """
    "Treat this one as a warning" has to mean every finding the rule raises.

    `CDN-006` is raised with an explicit severity by the download-ratio detector, so it is
    the case that proves the override is not quietly discarded.
    """
    set_severity_overrides({"CDN-006": Severity.INFO})

    finding = R.CDN_DOWNLOAD_RATIO.raise_finding(
        "A segment took 0.8 of its EXTINF to download.",
        severity=Severity.WARN,
        stream_layer=StreamLayer.PLAYBACK,
    )

    assert finding.severity is Severity.INFO


def test_a_rule_with_no_override_keeps_what_it_declares() -> None:
    set_severity_overrides({"HTTP-002": Severity.INFO})

    assert R.HTTP_REDIRECT_DEPTH.raise_finding("Three hops.").severity is (
        R.HTTP_REDIRECT_DEPTH.severity
    )


# -- the endpoints -----------------------------------------------------------


def _rule(api: TestClient, rule_id: str) -> dict[str, object]:
    body = api.get("/api/settings/rules").json()
    return next(rule for rule in body["rules"] if rule["id"] == rule_id)


def test_the_catalogue_states_both_the_declared_and_the_effective_severity(
    api: TestClient,
) -> None:
    before = _rule(api, "HTTP-002")
    assert (before["severity"], before["declared_severity"]) == ("WARN", "WARN")
    assert before["overridden"] is False

    written = api.put("/api/settings/rules/HTTP-002", json={"severity": "INFO"})
    assert written.status_code == 200

    after = _rule(api, "HTTP-002")
    assert (after["severity"], after["declared_severity"]) == ("INFO", "WARN")
    assert after["overridden"] is True, "a reader can see the rule was reassigned"
    assert written.json()["overridden_count"] == 1


def test_a_null_severity_restores_the_declared_one(api: TestClient) -> None:
    api.put("/api/settings/rules/HTTP-002", json={"severity": "CRITICAL"})
    assert _rule(api, "HTTP-002")["severity"] == "CRITICAL"

    api.put("/api/settings/rules/HTTP-002", json={"severity": None})

    restored = _rule(api, "HTTP-002")
    assert restored["severity"] == "WARN"
    assert restored["overridden"] is False


def test_reassigning_a_rule_to_what_it_already_declares_is_not_an_override(
    api: TestClient,
) -> None:
    """Otherwise the catalogue would mark a rule as reassigned when nothing changed."""
    api.put("/api/settings/rules/HTTP-002", json={"severity": "WARN"})

    assert _rule(api, "HTTP-002")["overridden"] is False
    assert severity_overrides() == {}


def test_a_severity_that_is_not_a_severity_is_refused(api: TestClient) -> None:
    response = api.put("/api/settings/rules/HTTP-002", json={"severity": "URGENT"})

    assert response.status_code == 400
    assert "is not a severity" in response.json()["detail"]
    assert _rule(api, "HTTP-002")["severity"] == "WARN", "nothing was changed"


def test_a_rule_that_does_not_exist_is_a_404(api: TestClient) -> None:
    response = api.put("/api/settings/rules/NOPE-001", json={"severity": "INFO"})

    assert response.status_code == 404
    assert "not a declared rule" in response.json()["detail"]


# -- what is stored ----------------------------------------------------------


@pytest.mark.asyncio()
async def test_a_stored_override_for_a_rule_that_no_longer_exists_is_dropped() -> None:
    """
    The catalogue is the authority on what exists.

    Carrying a stale row would silently reclassify whichever rule took that identifier next.
    """
    from app.api.settings import RULE_SEVERITY_KEY, _store, load_rule_severities_from_db

    await _store(RULE_SEVERITY_KEY, {"HTTP-002": "INFO", "GONE-999": "CRITICAL"})

    loaded = await load_rule_severities_from_db()

    assert loaded == {"HTTP-002": "INFO"}
    assert severity_overrides() == {"HTTP-002": Severity.INFO}
    await _store(RULE_SEVERITY_KEY, {})


@pytest.mark.asyncio()
async def test_a_stored_severity_that_is_not_a_severity_is_dropped() -> None:
    from app.api.settings import RULE_SEVERITY_KEY, _store, load_rule_severities_from_db

    await _store(RULE_SEVERITY_KEY, {"HTTP-002": "URGENT"})

    assert await load_rule_severities_from_db() == {}
    await _store(RULE_SEVERITY_KEY, {})


@pytest.mark.asyncio()
async def test_an_override_survives_a_restart(api: TestClient) -> None:
    """The store is what the next process reads, so the reassignment outlives this one."""
    from app.api.settings import load_rule_severities_from_db

    api.put("/api/settings/rules/SEG-008", json={"severity": "INFO"})
    set_severity_overrides({})  # as if the process had just started

    assert await load_rule_severities_from_db() == {"SEG-008": "INFO"}
    assert R.SEG_PTS_GAP.raise_finding("A gap.").severity is Severity.INFO

    api.put("/api/settings/rules/SEG-008", json={"severity": None})


# -- what a report says ------------------------------------------------------


def test_the_report_appendix_names_every_reassigned_severity() -> None:
    """
    A reader of an escalation has to see that a severity was changed on this deployment.

    A report that reads WARN where the catalogue declares CRITICAL, with nothing said, would
    understate the defect to whoever receives it.
    """
    from app.reports.render_html import _severity_overrides

    set_severity_overrides({"HTTP-002": Severity.INFO})

    rows = _severity_overrides()

    assert rows == [
        {
            "id": "HTTP-002",
            "title": "Redirect drops query parameters carried by the playback URL",
            "declared": "WARN",
            "effective": "INFO",
        }
    ]


def test_an_unreassigned_analyzer_puts_nothing_in_that_appendix() -> None:
    from app.reports.render_html import _severity_overrides

    assert _severity_overrides() == []

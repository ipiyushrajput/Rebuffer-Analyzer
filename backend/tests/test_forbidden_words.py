"""Deterministic wording (§5.4).

Every sentence RBA generates — rule titles, root causes, fixes, verdict text, report
templates and the frontend's fixed strings — states what was measured. Hedging words are
banned and this test fails the build if one enters the codebase.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.analysis.rules import catalogue  # noqa: F401 — importing declares the catalogue.
from app.analysis.rules.base import registry

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent

FORBIDDEN = [
    "may be",
    "maybe",
    "might",
    "possibly",
    "possible cause",
    "probably",
    "likely",
    "could be",
    "seems",
    "appears to",
    "appear to",
    "presumably",
    "perhaps",
    "we think",
    "suspect",
    "potentially",
]

_PATTERN = re.compile("|".join(re.escape(word) for word in FORBIDDEN), re.IGNORECASE)


def _offences(text: str) -> list[str]:
    return [m.group(0) for m in _PATTERN.finditer(text)]


@pytest.mark.parametrize("rule", registry.all(), ids=lambda r: r.id)
def test_rule_text_is_deterministic(rule) -> None:  # type: ignore[no-untyped-def]
    for field_name in ("title", "root_cause", "fix"):
        value = getattr(rule, field_name)
        offences = _offences(value)
        assert not offences, f"{rule.id}.{field_name} contains hedging words {offences}: {value}"


def test_every_rule_declares_a_root_cause_and_a_fix() -> None:
    for rule in registry.all():
        assert rule.root_cause.strip(), f"{rule.id} declares no root cause"
        assert rule.fix.strip(), f"{rule.id} declares no fix"
        assert rule.title.strip(), f"{rule.id} declares no title"
        assert rule.root_cause.rstrip().endswith("."), f"{rule.id} root cause is not a sentence"
        assert rule.fix.rstrip().endswith("."), f"{rule.id} fix is not a sentence"


def test_rule_ids_follow_the_prefix_scheme() -> None:
    valid_prefixes = {
        "NET",
        "TLS",
        "HTTP",
        "CDN",
        "MST",
        "MED",
        "SEQ",
        "SEG",
        "VID",
        "AUD",
        "AV",
        "SUB",
        "ADS",
        "PLY",
        "VPB",
        "INFO",
    }
    for rule in registry.all():
        prefix, _, number = rule.id.partition("-")
        assert prefix in valid_prefixes, f"{rule.id} uses an unknown prefix"
        assert number.isdigit(), f"{rule.id} does not end in a number"


def test_declared_thresholds_exist_in_the_configuration() -> None:
    from app.config import Thresholds

    fields = set(Thresholds.model_fields)
    for rule in registry.all():
        for name in rule.thresholds:
            assert name in fields, f"{rule.id} names a threshold that does not exist: {name}"


def _text_sources() -> list[Path]:
    paths: list[Path] = []
    reports = BACKEND_ROOT / "app" / "reports"
    if reports.exists():
        paths += list(reports.rglob("*.html"))
        paths += list(reports.rglob("*.j2"))
    analysis = BACKEND_ROOT / "app" / "analysis"
    paths += list(analysis.rglob("*.py"))
    return paths


def test_report_templates_and_analysis_text_are_deterministic() -> None:
    offenders: list[str] = []
    for path in _text_sources():
        if path.name == "test_forbidden_words.py":
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            # The ban applies to generated text, not to identifiers such as `likely_cause`.
            stripped = re.sub(r"[A-Za-z0-9_]*_(?:likely|maybe|possible)[A-Za-z0-9_]*", "", line)
            for word in _offences(stripped):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_number}: {word!r} in {line.strip()}"
                )
    assert not offenders, "Hedging words found in generated text:\n" + "\n".join(offenders)

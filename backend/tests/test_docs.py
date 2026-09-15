"""The documentation must describe the software that exists.

`docs/REFERENCE_TOOLS.md` claims a rule covers each capability of the market tools, and
`docs/RULES.md` is generated from the registry. Both are checked against the registry here,
so a rule that is renamed or removed fails the build instead of leaving a dead claim in the
coverage matrix.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.analysis.rules import catalogue  # noqa: F401 — importing declares the catalogue.
from app.analysis.rules.base import registry

DOCS = Path(__file__).resolve().parent.parent.parent / "docs"
RULE_ID = re.compile(
    r"`((?:NET|TLS|HTTP|CDN|MST|MED|SEQ|SEG|VID|AUD|AV|SUB|ADS|PLY|VPB|INFO)-\d{3})`"
)


def _referenced_ids(path: Path) -> set[str]:
    return set(RULE_ID.findall(path.read_text(encoding="utf-8")))


def test_reference_tools_matrix_only_names_rules_that_exist() -> None:
    path = DOCS / "REFERENCE_TOOLS.md"
    assert path.exists(), "docs/REFERENCE_TOOLS.md is missing"

    referenced = _referenced_ids(path)
    assert referenced, "the coverage matrix names no rule"

    known = {rule.id for rule in registry.all()}
    missing = sorted(referenced - known)
    assert not missing, f"the coverage matrix names rules that do not exist: {missing}"


def test_every_reference_tool_has_a_section() -> None:
    text = (DOCS / "REFERENCE_TOOLS.md").read_text(encoding="utf-8")
    for tool in (
        "HLSAnalyzer.com",
        "Qosifire",
        "Dolby Stream Validator",
        "Akamai Stream Validator",
        "THEOplayer Inspect Stream",
    ):
        assert tool in text, f"{tool} has no section in the coverage matrix"
    assert "Out of scope, with the reason" in text


def test_rules_md_is_generated_from_the_registry() -> None:
    path = DOCS / "RULES.md"
    assert path.exists(), "docs/RULES.md is missing; run `make rules`"
    text = path.read_text(encoding="utf-8")

    assert text.startswith("# Rule catalogue")
    assert f"{len(registry)} rules" in text, "docs/RULES.md is stale; run `make rules`"

    documented = _referenced_ids(path)
    known = {rule.id for rule in registry.all()}
    undocumented = sorted(known - documented)
    assert not undocumented, f"docs/RULES.md is stale; run `make rules`. Missing: {undocumented}"


@pytest.mark.parametrize("rule", registry.all(), ids=lambda r: r.id)
def test_every_rule_carries_a_root_cause_and_fix_in_the_catalogue(rule) -> None:  # type: ignore[no-untyped-def]
    text = (DOCS / "RULES.md").read_text(encoding="utf-8")
    assert rule.root_cause in text, f"{rule.id} root cause is absent from docs/RULES.md"
    assert rule.fix in text, f"{rule.id} fix is absent from docs/RULES.md"


def test_the_readme_names_the_deployment_ports_and_the_reserved_one() -> None:
    readme = (DOCS.parent / "README.md").read_text(encoding="utf-8")
    assert "8010" in readme
    assert "8080" in readme
    assert "8001" in readme, "the README must record that 8001 belongs to Metanalyser"
    assert "107.109.131.68" in readme


def test_no_committed_file_carries_an_ai_attribution_line() -> None:
    root = DOCS.parent
    patterns = (
        re.compile(r"^Co-Authored-By:.*(claude|anthropic)", re.IGNORECASE | re.MULTILINE),
        re.compile(r"Generated with \[Claude Code\]", re.IGNORECASE),
    )
    offenders: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.parts)
        if parts & {".git", "node_modules", ".venv", "dist", "__pycache__"}:
            continue
        if path.name == "test_docs.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in patterns:
            if pattern.search(text):
                offenders.append(str(path.relative_to(root)))
    assert not offenders, f"attribution lines found in: {sorted(set(offenders))}"


def test_every_threshold_is_editable_in_the_settings_tab() -> None:
    """A threshold the Settings tab does not list cannot be changed without a code change.

    The tab renders explicit key groups, so a threshold added to `Thresholds` and left out of
    them is invisible on screen while still governing what fires.
    """
    from app.config import Thresholds

    settings_tsx = (DOCS.parent / "frontend" / "src" / "tabs" / "Settings.tsx").read_text(
        encoding="utf-8"
    )
    missing = [name for name in Thresholds.model_fields if f"'{name}'" not in settings_tsx]
    assert not missing, f"thresholds absent from the Settings tab groups: {missing}"

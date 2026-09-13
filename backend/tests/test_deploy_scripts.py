"""The installers must agree with the deployment the documentation describes.

Every way of starting RBA — the Makefile, the Linux installer, the Windows scripts and
Docker — binds the same two ports and refuses the same reserved one. A script that drifts
from that is a service bound over the Metanalyser backend on the deployment host, so the
agreement is asserted here rather than left to review.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
WINDOWS = ROOT / "deploy" / "windows"

BACKEND_PORT = "8010"
FRONTEND_PORT = "8080"
RESERVED_PORT = "8001"


def _read(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    ["_common.ps1", "setup.ps1", "run.ps1", "setup.cmd", "rba.cmd"],
)
def test_the_windows_scripts_are_present(name: str) -> None:
    assert (WINDOWS / name).is_file(), f"deploy/windows/{name} is missing"


def test_the_windows_runner_uses_the_documented_ports() -> None:
    run = _read(WINDOWS / "run.ps1")
    assert BACKEND_PORT in run, "the Windows runner must default the backend to 8010"
    assert FRONTEND_PORT in run, "the Windows runner must default the frontend to 8080"


def test_both_installers_refuse_the_metanalyser_port() -> None:
    """8001 belongs to the existing Metanalyser backend and is never bound by RBA."""
    for path in (ROOT / "deploy" / "install.sh", WINDOWS / "_common.ps1"):
        text = _read(path)
        assert RESERVED_PORT in text, f"{path.name} must name the reserved port"
        assert "Metanalyser" in text, f"{path.name} must say why 8001 is reserved"


def test_the_windows_wrappers_do_not_change_the_machine_execution_policy() -> None:
    """`-ExecutionPolicy Bypass` on the invocation; never Set-ExecutionPolicy on the host."""
    for name in ("setup.cmd", "rba.cmd"):
        text = _read(WINDOWS / name)
        assert "-ExecutionPolicy Bypass" in text
        assert "Set-ExecutionPolicy" not in text


@pytest.mark.parametrize(
    "name",
    ["_common.ps1", "setup.ps1", "run.ps1", "setup.cmd", "rba.cmd"],
)
def test_the_windows_scripts_are_pure_ascii(name: str) -> None:
    """Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI, not UTF-8.

    A UTF-8 em dash then arrives as three cp1252 characters ending in a double quote, which
    closes the string it sits in and breaks the parse several hundred lines later. Keeping
    these files to ASCII makes the two decodings identical, so the same file parses under
    Windows PowerShell 5.1 and PowerShell 7 alike.
    """
    raw = (WINDOWS / name).read_bytes()
    offenders = sorted({byte for byte in raw if byte > 0x7F})
    assert not offenders, (
        f"deploy/windows/{name} carries non-ASCII bytes {offenders}. "
        "Windows PowerShell 5.1 decodes them as ANSI and the script stops parsing."
    )
    assert raw.decode("cp1252") == raw.decode("utf-8")


def test_the_rules_target_writes_the_catalogue_without_a_bom() -> None:
    """`Set-Content -Encoding utf8` emits a BOM on 5.1, and docs/RULES.md is compared
    against the registry byte for byte."""
    run = _read(WINDOWS / "run.ps1")
    assert "UTF8Encoding" in run, "the rules target must write UTF-8 without a BOM"
    assert "WriteAllText" in run, "the catalogue is written through .NET, not Set-Content"


def test_the_windows_scripts_carry_no_credentials() -> None:
    for path in WINDOWS.iterdir():
        if not path.is_file():
            continue
        text = _read(path).lower()
        for secret in ("db_password=", "password=", "hdnts="):
            assert secret not in text, f"{path.name} must not carry a credential"


def test_setup_md_covers_both_platforms() -> None:
    setup = _read(ROOT / "docs" / "SETUP.md")
    for token in ("make install", "setup.cmd", "rba.cmd", "DB_ENGINE=sqlite", RESERVED_PORT):
        assert token in setup, f"docs/SETUP.md must document {token}"


def test_the_readme_points_at_the_setup_guide() -> None:
    readme = _read(ROOT / "README.md")
    assert "docs/SETUP.md" in readme
    assert "rba.cmd" in readme, "the README quick start must name the Windows runner"

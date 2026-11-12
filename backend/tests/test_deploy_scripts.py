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


def test_the_windows_runner_exposes_every_cli_command() -> None:
    """A CLI command the operator needs on Windows is only real once run.ps1 offers it."""
    run = _read(WINDOWS / "run.ps1")
    for command in ("analyse", "diagnose", "rules"):
        assert f"app.cli {command}" in run, f"run.ps1 must run `app.cli {command}`"
        assert f"'{command}'" in run, f"run.ps1 must accept the {command} target"


def test_both_installers_refuse_the_metanalyser_port() -> None:
    """8001 belongs to the existing Metanalyser backend and is never bound by RBA."""
    for path in (ROOT / "deploy" / "install.sh", WINDOWS / "_common.ps1"):
        text = _read(path)
        assert RESERVED_PORT in text, f"{path.name} must name the reserved port"
        assert "Metanalyser" in text, f"{path.name} must say why 8001 is reserved"


def test_setup_rebuilds_a_venv_whose_pip_does_not_run() -> None:
    """A damaged pip fails every run of setup at its first pip call; setup checks it first."""
    setup = _read(WINDOWS / "setup.ps1")
    common = _read(WINDOWS / "_common.ps1")
    assert "[switch]$Fresh" in setup
    assert "Test-VenvPip" in setup and "function Test-VenvPip" in common
    assert "Remove-Venv" in setup and "function Remove-Venv" in common
    # The pip check runs before the first pip call it protects.
    assert setup.index("Test-VenvPip") < setup.index("-m pip install --upgrade pip")
    assert "-Fresh" in _read(WINDOWS / "setup.cmd")


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


def test_the_makefile_offers_every_windows_target() -> None:
    """`rba.cmd` mirrors the Makefile target for target; Ubuntu gets the same commands."""
    import re

    run = _read(WINDOWS / "run.ps1")
    declared = re.search(r"ValidateSet\(([^)]*)\)", run)
    assert declared is not None
    targets = {name.strip().strip("'") for name in declared.group(1).split(",")}
    makefile = _read(ROOT / "Makefile")
    # `setup` re-runs the Windows installer; the Makefile's name for that is `install`.
    for target in [*sorted(targets - {"setup"}), "install"]:
        assert re.search(rf"^{target}:", makefile, re.M), f"the Makefile has no `{target}` target"


def test_the_ubuntu_installer_keeps_the_host_env_and_fails_loudly() -> None:
    install = _read(ROOT / "deploy" / "install.sh")
    # An upgrade copies the checkout over /opt/rba/backend with --delete; the host's .env,
    # which the checkout does not carry, would go with it.
    assert "--exclude '.env'" in install
    assert 'if [ ! -f "$PREFIX/backend/.env" ]' in install
    # `nginx -t && reload` under `set -e` reports Done over a site nginx refused.
    assert "nginx -t || fail" in install
    # The page calls /api on its own origin, so one build serves the IP, a hostname and HTTPS.
    assert "107.109.131.68:${FRONTEND_PORT}/api" not in install
    assert "sys.version_info < (3, 11)" in install, "the installer must refuse Python < 3.11"


def test_chromium_is_installed_where_the_service_can_read_it() -> None:
    """ProtectHome hides root's ~/.cache/ms-playwright from the service."""
    install = _read(ROOT / "deploy" / "install.sh")
    unit = _read(ROOT / "deploy" / "rba-backend.service")
    assert 'BROWSERS="$PREFIX/browsers"' in install
    assert 'PLAYWRIGHT_BROWSERS_PATH="$BROWSERS"' in install
    assert "ProtectHome=yes" in unit
    assert "Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/rba/browsers" in unit


def test_the_ubuntu_prerequisites_meet_the_documented_versions() -> None:
    prereqs = ROOT / "deploy" / "prereqs-ubuntu.sh"
    text = _read(prereqs)
    assert prereqs.stat().st_mode & 0o111, "deploy/prereqs-ubuntu.sh must be executable"
    assert "sys.version_info < (3, 11)" in text
    assert "deadsnakes" in text, "Ubuntu 22.04's python3 is 3.10"
    assert "setup_22.x" in text, "Ubuntu's own nodejs is older than 20"
    for token in ("prereqs-ubuntu.sh", "deploy/install.sh", "systemctl", "journalctl"):
        assert token in _read(ROOT / "docs" / "SETUP.md"), f"docs/SETUP.md must document {token}"


SNIPPET = ROOT / "frontend" / "nginx" / "streamlens-locations.conf.template"


def test_one_nginx_snippet_serves_every_streamlens_path_under_the_prefix() -> None:
    """The UI, the API, the WebSockets and both health paths live below the base path, and
    nginx strips it on the way to the backend, which serves /api and /ws unprefixed."""
    snippet = _read(SNIPPET)
    base = "${STREAMLENS_BASE_PATH}"
    backend = "http://${STREAMLENS_BACKEND}"
    for location in (
        f"location = {base} {{",
        f"location = {base}/healthz {{",
        f"location = {base}/health {{",
        f"location {base}/api/ {{",
        f"location {base}/ws/ {{",
        f"location {base}/ {{",
    ):
        assert location in snippet, f"the snippet must declare `{location}`"
    assert f"proxy_pass {backend}/api/;" in snippet
    assert f"proxy_pass {backend}/ws/;" in snippet
    assert f"proxy_pass {backend}/api/health;" in snippet
    assert 'proxy_set_header Connection "upgrade";' in snippet
    # The backend's address is a parameter, never a fixed host: the two machines may be apart.
    directives = [line for line in snippet.splitlines() if not line.lstrip().startswith("#")]
    assert not any("127.0.0.1" in line for line in directives)
    # Behind an HTTPS load balancer or a mapped container port, an absolute redirect names the
    # wrong scheme or port.
    assert snippet.count("absolute_redirect off;") >= 2


def test_every_layout_mounts_the_same_snippet_and_none_serves_the_ui_at_the_root() -> None:
    site = _read(ROOT / "deploy" / "rba-frontend.nginx.conf")
    docker_site = _read(ROOT / "frontend" / "nginx.conf")
    dockerfile = _read(ROOT / "frontend" / "Dockerfile")
    install = _read(ROOT / "deploy" / "install.sh")

    assert "include /etc/nginx/snippets/streamlens.conf;" in site
    assert "include /etc/nginx/streamlens/locations.conf;" in docker_site
    assert "frontend/nginx/streamlens-locations.conf.template" in install
    assert "nginx/streamlens-locations.conf.template /etc/nginx/templates/" in dockerfile
    # The image's entrypoint refuses to render into a directory that is absent.
    assert "mkdir -p /etc/nginx/streamlens" in dockerfile
    for text in (site, docker_site):
        assert "return 302 /streamlens/;" in text
        assert "try_files" not in text, "the UI is served by the snippet only, under the prefix"


def test_the_installer_supports_one_machine_and_two() -> None:
    install = _read(ROOT / "deploy" / "install.sh")
    for option in ("--role", "--backend", "--base-path", "--snippet-only"):
        assert option in install, f"install.sh must accept {option}"
    assert "all|backend|frontend" in install
    assert 'BACKEND="127.0.0.1:${BACKEND_PORT}"' in install, "one machine stays the default"
    assert "--role frontend needs --backend" in install
    assert 'VITE_BASE_PATH="${BASE_PATH}/" npm run build' in install
    # A reload returns before the new workers answer; checking at once reads the old config.
    reload_at = install.index("\n  systemctl reload nginx\n")
    assert "\n  sleep " in install[reload_at : reload_at + 300]


def test_the_frontend_is_built_under_streamlens_by_default() -> None:
    vite = _read(ROOT / "frontend" / "vite.config.ts")
    assert "process.env.VITE_BASE_PATH || '/streamlens/'" in vite
    assert "rewrite: strip" in vite, "the dev proxies strip the prefix as nginx does"
    compose = _read(ROOT / "docker-compose.yml")
    assert "VITE_BASE_PATH: /streamlens/" in compose
    assert "STREAMLENS_BACKEND: ${STREAMLENS_BACKEND:-backend:8010}" in compose
    assert "107.109.131.68" not in compose, "no address is built into the bundle"

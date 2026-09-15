"""PDF rendering.

Playwright prints the same HTML the browser shows, so the PDF and the HTML carry identical
content and identical charts. When Playwright is absent the caller is told so as a fact and
the HTML report is still produced.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Typed loosely on purpose: Playwright declares this as a TypedDict that mypy cannot
# match against a plain literal without repeating the import at module scope.
PDF_MARGIN: Any = {"top": "12mm", "bottom": "14mm", "left": "10mm", "right": "10mm"}
RENDER_TIMEOUT_MS = 60_000


def _browser_roots() -> list[Path]:
    """The directories Playwright keeps its browsers in, per platform."""
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return [Path(configured)]
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return [Path(local) / "ms-playwright"] if local else []
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Caches" / "ms-playwright"]
    return [Path("/opt/pw-browsers"), Path.home() / ".cache" / "ms-playwright"]


def chromium_executable() -> str | None:
    """Locate a Chromium the host already has.

    Playwright refuses to launch when the installed browser build differs from the one its
    Python package pins, which happens on a host where the browsers were provisioned
    separately. When that is the case the binary is named explicitly instead.
    """
    configured = os.environ.get("RBA_CHROMIUM_EXECUTABLE")
    if configured and Path(configured).exists():
        return configured

    # Playwright names the directory after the platform it built for.
    if sys.platform == "win32":
        patterns = [
            "chromium-*/chrome-win/chrome.exe",
            "chromium_headless_shell-*/chrome-win/*.exe",
        ]
    elif sys.platform == "darwin":
        patterns = [
            "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
            "chromium_headless_shell-*/chrome-mac/*",
        ]
    else:
        patterns = ["chromium-*/chrome-linux/chrome", "chromium_headless_shell-*/chrome-linux/*"]

    for root in _browser_roots():
        if not root.is_dir():
            continue
        for pattern in patterns:
            for candidate in sorted(root.glob(pattern), reverse=True):
                if candidate.is_file():
                    return str(candidate)
    return None


class PdfUnavailable(RuntimeError):
    """Raised when Playwright or its browser is not installed."""


PDF_FOOTER = (
    '<div style="width:100%;font-size:8px;color:#667085;padding:0 10mm;'
    'display:flex;justify-content:space-between">'
    "<span>TV Plus Rebuffer Analyzer</span>"
    '<span class="pageNumber"></span>/<span class="totalPages"></span></div>'
)


def render_pdf_blocking(html: str) -> bytes:
    """Print ``html`` and return the PDF bytes, synchronously.

    Playwright's **sync** API is used on purpose. The async API drives the browser over an
    asyncio subprocess, and on Windows a server running on the selector event loop cannot
    spawn one at all — `_make_subprocess_transport` raises NotImplementedError and the PDF
    never renders. The sync API owns its own machinery, so it works whatever loop the server
    happens to be on. `render_pdf_bytes` calls this on a worker thread, where no event loop
    is running, which is the one condition the sync API asks for.

    The HTML is loaded from a temporary file so relative resources and the inlined chart
    script execute exactly as they do in a browser; nothing else touches the disk, which is
    what lets a report be stored in the database rather than on the host.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise PdfUnavailable(
            "Playwright is not installed on the analyzer host, so PDF export did not run. "
            "The HTML report carries the same content."
        ) from exc

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as handle:
        handle.write(html)
        source = Path(handle.name)

    try:
        with sync_playwright() as playwright:
            launch: dict[str, Any] = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            try:
                browser = playwright.chromium.launch(**launch)
            except Exception:  # Retry once with an explicitly named binary.
                executable = chromium_executable()
                if executable is None:
                    raise
                logger.info("launching the Chromium found at %s", executable)
                browser = playwright.chromium.launch(executable_path=executable, **launch)
            try:
                page = browser.new_page()
                page.goto(source.as_uri(), wait_until="networkidle", timeout=RENDER_TIMEOUT_MS)
                # The charts render after the inlined script runs; give them one frame.
                page.wait_for_timeout(600)
                pdf = page.pdf(
                    format="A4",
                    print_background=True,
                    margin=PDF_MARGIN,
                    display_header_footer=True,
                    header_template="<div></div>",
                    footer_template=PDF_FOOTER,
                )
            finally:
                browser.close()
    except PdfUnavailable:
        raise
    except Exception as exc:
        raise PdfUnavailable(
            f"The headless browser did not produce a PDF: {type(exc).__name__}: {exc}. "
            "The HTML report carries the same content."
        ) from exc
    finally:
        source.unlink(missing_ok=True)

    return bytes(pdf)


async def render_pdf_bytes(html: str) -> bytes:
    """Print ``html`` on a worker thread and return the PDF bytes."""
    return await asyncio.to_thread(render_pdf_blocking, html)


async def render_pdf(html: str, destination: Path) -> Path:
    """Print ``html`` to ``destination``, for the CLI and for a disk-mirroring deployment."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(await render_pdf_bytes(html))
    return destination


def render_pdf_sync(html: str, destination: Path) -> Path:
    """Blocking wrapper for the CLI. No event loop is involved."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(render_pdf_blocking(html))
    return destination


def available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True

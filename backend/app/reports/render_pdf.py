"""PDF rendering.

Playwright prints the same HTML the browser shows, so the PDF and the HTML carry identical
content and identical charts. When Playwright is absent the caller is told so as a fact and
the HTML report is still produced.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

PDF_MARGIN = {"top": "12mm", "bottom": "14mm", "left": "10mm", "right": "10mm"}
RENDER_TIMEOUT_MS = 60_000


class PdfUnavailable(RuntimeError):
    """Raised when Playwright or its browser is not installed."""


async def render_pdf(html: str, destination: Path) -> Path:
    """Print ``html`` to ``destination``. The HTML is loaded from a file so relative
    resources and the inlined chart script both execute exactly as they do in a browser."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise PdfUnavailable(
            "Playwright is not installed on the analyzer host, so PDF export did not run. "
            "The HTML report carries the same content."
        ) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as handle:
        handle.write(html)
        source = Path(handle.name)

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=["--no-sandbox"])
            try:
                page = await browser.new_page()
                await page.goto(
                    source.as_uri(), wait_until="networkidle", timeout=RENDER_TIMEOUT_MS
                )
                # The charts render after the inlined script runs; give them one frame.
                await page.wait_for_timeout(600)
                await page.pdf(
                    path=str(destination),
                    format="A4",
                    print_background=True,
                    margin=PDF_MARGIN,
                    display_header_footer=True,
                    header_template="<div></div>",
                    footer_template=(
                        '<div style="width:100%;font-size:8px;color:#5c6478;padding:0 10mm;'
                        'display:flex;justify-content:space-between">'
                        "<span>TV Plus Rebuffer Analyzer</span>"
                        '<span class="pageNumber"></span>/<span class="totalPages"></span></div>'
                    ),
                )
            finally:
                await browser.close()
    except Exception as exc:
        raise PdfUnavailable(
            f"The headless browser did not produce a PDF: {type(exc).__name__}. "
            "The HTML report carries the same content."
        ) from exc
    finally:
        source.unlink(missing_ok=True)

    return destination


def render_pdf_sync(html: str, destination: Path) -> Path:
    """Blocking wrapper for the CLI."""
    return asyncio.run(render_pdf(html, destination))


def available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True

"""PDF export must not depend on the event loop the server happens to run on.

Playwright drives the browser through a subprocess. The async API asks asyncio to spawn it,
and on Windows a server on the selector event loop cannot: `_make_subprocess_transport`
raises NotImplementedError and the export fails with a traceback the operator can do nothing
about. Rendering on a worker thread through the sync API removes the dependency entirely.
"""

from __future__ import annotations

import asyncio

import pytest

from app.reports.render_pdf import (
    PdfUnavailable,
    available,
    render_pdf_blocking,
    render_pdf_bytes,
)

HTML = "<!doctype html><html><head><title>t</title></head><body><h1>Export</h1></body></html>"

needs_browser = pytest.mark.skipif(
    not available(), reason="Playwright is not installed on this host"
)


class _NoSubprocessLoop(asyncio.SelectorEventLoop):
    """A loop shaped like Windows' selector loop: it refuses to spawn a subprocess."""

    def _make_subprocess_transport(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError


@needs_browser
def test_the_blocking_renderer_produces_a_pdf() -> None:
    data = render_pdf_blocking(HTML)
    assert data.startswith(b"%PDF-")


@needs_browser
def test_a_pdf_renders_from_inside_a_running_event_loop() -> None:
    """The server's case: the request handler is already on a loop."""
    data = asyncio.run(render_pdf_bytes(HTML))
    assert data.startswith(b"%PDF-")


@needs_browser
def test_a_pdf_renders_on_a_loop_that_cannot_spawn_subprocesses() -> None:
    """The Windows failure the operator reported, reproduced and shown fixed."""
    loop = _NoSubprocessLoop()
    try:
        data = loop.run_until_complete(render_pdf_bytes(HTML))
    finally:
        loop.close()
    assert data.startswith(b"%PDF-")


def test_a_missing_browser_is_reported_as_a_fact_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTML report still carries the content, and the caller is told why."""
    import builtins

    real_import = builtins.__import__

    def refuse(name: str, *args: object, **kwargs: object) -> object:
        if name == "playwright.sync_api":
            raise ImportError("playwright is not installed")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", refuse)

    with pytest.raises(PdfUnavailable) as raised:
        render_pdf_blocking(HTML)
    assert "HTML report carries the same content" in str(raised.value)

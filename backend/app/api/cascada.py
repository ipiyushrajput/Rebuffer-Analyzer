"""The CASCADA Data tab's API.

Every call to CASCADA goes out from here, never from the browser: the API needs a session
cookie, sends no CORS headers, and the cookie must never reach a page. Nothing on this router
returns the session — `GET /cascada/session` returns a masked tail and when it was last
validated, which is what the Settings panel shows.

An expired session is answered with HTTP 401 and a sentence naming the remedy, so every
CASCADA surface can show one banner instead of failing in a different way per screen.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.cascada import client, exports, service, store
from app.cascada.auth import (
    ACCEPTED_COOKIES,
    CSRF_COOKIE,
    SESSION_COOKIE,
    CascadaAuthError,
    clear_stored_session,
    parse_cookie_header,
    resolve_auth,
    save_stored_session,
)
from app.cascada.client import CascadaError
from app.config import get_thresholds
from app.tvplus import catalogue as cat

router = APIRouter(prefix="/cascada", tags=["cascada"])

CSV_MEDIA = "text/csv"
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _attachment(data: bytes, media: str, filename: str) -> Response:
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _auth_http(exc: CascadaAuthError) -> HTTPException:
    """A rejected session is 401 everywhere, so the UI has one case to render."""
    return HTTPException(status_code=401, detail=str(exc))


# -- the session -------------------------------------------------------------


class SessionIn(BaseModel):
    """A pasted CASCADA session: a whole Cookie header, or the cookies on their own."""

    cookie_header: str = Field(default="", description="A Cookie header line pasted verbatim")
    sessionid: str = Field(default="")
    csrftoken: str = Field(default="")


@router.get("/session")
async def read_session() -> dict[str, Any]:
    """What is known about the configured session. Never the session itself."""
    auth = await resolve_auth()
    return auth.describe().as_dict()


@router.put("/session")
async def write_session(body: SessionIn) -> dict[str, Any]:
    """Store a pasted session, after proving it works with one live call."""
    cookies = parse_cookie_header(body.cookie_header) if body.cookie_header.strip() else {}
    if body.sessionid.strip():
        cookies[SESSION_COOKIE] = body.sessionid.strip()
    if body.csrftoken.strip():
        cookies[CSRF_COOKIE] = body.csrftoken.strip()
    cookies = {name: value for name, value in cookies.items() if name in ACCEPTED_COOKIES}

    if not cookies.get(SESSION_COOKIE):
        raise HTTPException(
            status_code=400,
            detail=(
                "The pasted text carries no `sessionid` cookie. In a signed-in CASCADA tab, "
                "open devtools, copy the Cookie header from any request to "
                "cascada.samsungcloud.tv, and paste the whole line here."
            ),
        )

    # The session is validated before it is stored, so the panel never shows a green state
    # for a session that does not work.
    from app.cascada.auth import _CookieAuth

    candidate = _CookieAuth(cookies=cookies, source="stored")
    thresholds = get_thresholds()
    try:
        detail = await client.validate(candidate, thresholds)
        valid = True
    except CascadaAuthError as exc:
        raise _auth_http(exc) from exc
    except CascadaError as exc:
        # The session may be sound and CASCADA merely unreachable, so it is stored with what
        # happened rather than refused.
        detail, valid = str(exc), False

    try:
        stored = await save_stored_session(cookies, valid=valid, detail=detail)
    except CascadaAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return stored.describe().as_dict()


@router.delete("/session")
async def delete_session() -> dict[str, Any]:
    """Forget the pasted session. A host-configured one is in the environment, not here."""
    await clear_stored_session()
    return (await resolve_auth()).describe().as_dict()


@router.post("/session/validate")
async def validate_session() -> dict[str, Any]:
    """Re-check the configured session against CASCADA, and record what was found."""
    from app.cascada.auth import mark_stored_session

    auth = await resolve_auth()
    thresholds = get_thresholds()
    try:
        detail = await client.validate(auth, thresholds)
    except CascadaAuthError as exc:
        await mark_stored_session(valid=False, detail=str(exc))
        raise _auth_http(exc) from exc
    except CascadaError as exc:
        await mark_stored_session(valid=False, detail=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await mark_stored_session(valid=True, detail=detail)
    return (await resolve_auth()).describe().as_dict()


# -- one channel -------------------------------------------------------------


async def _window_for_request(
    service_id: str, channel_name: str, country: str, refresh: bool
) -> store.ChannelWindow:
    try:
        return await service.channel_window(
            service_id=service_id,
            channel_name=channel_name,
            country=country,
            refresh=refresh,
        )
    except CascadaAuthError as exc:
        raise _auth_http(exc) from exc
    except CascadaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/channel")
async def read_channel(
    service_id: str = Query(..., min_length=1, description="The channel's SVC_ID"),
    channel_name: str = Query(..., min_length=1),
    country: str = Query("", max_length=8),
    refresh: bool = Query(False, description="Ignore the stored window and ask CASCADA again"),
) -> dict[str, Any]:
    """One channel's rebuffering series, with the current week and the week before it."""
    entry = await _window_for_request(service_id, channel_name, country, refresh)
    return entry.as_dict()


@router.get("/channel/report.{fmt}")
async def download_channel_report(
    fmt: str,
    service_id: str = Query(..., min_length=1),
    channel_name: str = Query(..., min_length=1),
    country: str = Query("", max_length=8),
) -> Response:
    """One channel's summary and per-minute rows, as CSV or XLSX."""
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="Format must be csv or xlsx")
    entry = await _window_for_request(service_id, channel_name, country, refresh=False)
    if fmt == "csv":
        return _attachment(
            exports.channel_csv(entry).encode("utf-8"),
            CSV_MEDIA,
            exports.channel_filename(service_id, "csv"),
        )
    return _attachment(
        exports.channel_xlsx(entry), XLSX_MEDIA, exports.channel_filename(service_id, "xlsx")
    )


# -- the country scan --------------------------------------------------------


class ScanIn(BaseModel):
    country: str = Field(..., min_length=2, max_length=2)
    concurrency: int | None = Field(default=None, ge=1, le=16)


@router.post("/scans", status_code=201)
async def start_scan(body: ScanIn) -> dict[str, Any]:
    """Measure every channel in one country. The response is the scan's opening state."""
    try:
        scan = await service.start_scan(body.country, body.concurrency)
    except CascadaAuthError as exc:
        raise _auth_http(exc) from exc
    except cat.CatalogueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except service.CascadaScanError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return scan.as_dict()


@router.get("/scans/{scan_id}")
async def read_scan(scan_id: str) -> dict[str, Any]:
    """A scan's progress, the channels it has found above threshold, and what failed."""
    scan = service.get_scan(scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="That scan is not running on this host")
    return scan.as_dict()


@router.delete("/scans/{scan_id}")
async def cancel_scan(scan_id: str) -> dict[str, Any]:
    """Stop a scan. Everything it measured is kept."""
    scan = await service.cancel_scan(scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="That scan is not running on this host")
    return scan.as_dict()


@router.get("/scans/{scan_id}/report.{fmt}")
async def download_country_report(scan_id: str, fmt: str) -> Response:
    """The country's rebuffering channels, worst first, as CSV or XLSX."""
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="Format must be csv or xlsx")
    scan = service.get_scan(scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="That scan is not running on this host")

    above = scan.above
    partial = scan.status == "RUNNING" or bool(scan.failures)
    scanned = (
        f"{scan.done} of {scan.total} channel(s) measured, {len(scan.failures)} failed, "
        f"status {scan.status}"
    )
    kwargs: dict[str, Any] = {
        "country": scan.country,
        "window": scan.window,
        "threshold_pct": scan.threshold_pct,
        "partial": partial,
        "scanned": scanned,
        # The catalogue's playback URL for each channel, so the report is enough on its own to
        # hand a channel to whoever has to analyse it.
        "urls": scan.urls,
    }
    if fmt == "csv":
        return _attachment(
            exports.country_csv(above, **kwargs).encode("utf-8"),
            CSV_MEDIA,
            exports.country_filename(scan.country, "csv"),
        )
    return _attachment(
        exports.country_xlsx(above, **kwargs),
        XLSX_MEDIA,
        exports.country_filename(scan.country, "xlsx"),
    )

"""The Samsung TV Plus channel catalogue, served to the All channels tab.

The catalogue is fetched here rather than in the browser: the URL carries the environment
mapping and the date the operator searched, the origin sends no CORS headers, and the
request needs the analyzer's fetcher, which is the one place TLS verification is disabled.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.net.fetcher import Fetcher
from app.tvplus import catalogue as cat

router = APIRouter(prefix="/catalogue", tags=["catalogue"])

REQUEST_TIMEOUT_S = 20.0


@router.get("/countries")
async def list_countries() -> dict[str, Any]:
    """Every selectable country and environment, so the tab holds no copy of the mapping."""
    return {
        "countries": [c.as_dict() for c in cat.COUNTRIES],
        "environments": list(cat.ENVIRONMENTS),
        "page_size": cat.PAGE_SIZE,
    }


@router.get("/channels")
async def list_channels(
    country: str = Query(..., min_length=2, max_length=2, description="Country code, e.g. AU"),
    env: str = Query("PRD", description="PRD or STG"),
    page: int = Query(1, ge=1),
    today: str | None = Query(
        None,
        pattern=r"^\d{8}$",
        description="YYYYMMDD. Sent back by a search so paging keeps that search's date.",
    ),
) -> dict[str, Any]:
    """One page of the live channel list for a country in an environment."""
    try:
        stamp = today or cat.today_stamp()
        url = cat.build_url(code=country, environment=env, page=page, today=stamp)
    except cat.CatalogueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    settings = get_settings()
    fetcher = Fetcher(
        timeout_s=REQUEST_TIMEOUT_S, per_host_connections=settings.rba_per_host_connections
    )
    try:
        result = await fetcher.fetch(url)
    finally:
        await fetcher.aclose()

    if result.error is not None:
        raise HTTPException(
            status_code=502,
            detail=f"The channel catalogue did not answer: {result.error}.",
        )
    if not result.ok:
        body = result.text.strip().replace("\n", " ")[:200]
        raise HTTPException(
            status_code=502,
            detail=(
                f"The channel catalogue answered HTTP {result.status} for {country.upper()} "
                f"{env.upper()} page {page}." + (f" It said: {body}" if body else "")
            ),
        )

    try:
        parsed = cat.parse_page(
            result.text, page=page, page_size=cat.PAGE_SIZE, url=url, today=stamp
        )
    except cat.CatalogueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        **parsed.as_dict(),
        "country": cat.country(country).as_dict(),
        "environment": env.upper(),
    }

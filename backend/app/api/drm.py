"""DRM API: what this deployment is configured with, what a URL needs, and the licence relay.

Three things, and between them an analyst never fills anything in for a protected channel:

* `GET /api/drm/settings` and `PUT /api/drm/settings` — the deployment's configuration,
  set once in Settings → DRM. The response says whether each credential is set, never what
  it is.
* `GET /api/drm/probe` — what one playback URL is protected with and what the player needs
  to play it. The tab calls this when a URL is entered and configures the player from the
  answer, so the same form serves a clear channel and a protected one.
* `POST /api/drm/license` — the Widevine licence relay. A browser cannot reach the licence
  server itself: it sends no CORS headers, and the deployment host is the one with a route to
  it. The challenge goes out through the project's own fetcher, so the licence request is
  made with the same TLS behaviour and the same manual redirect handling as everything else.

Nothing here returns key material, a private key, or the whereabouts of one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.drm import settings as drm_settings
from app.drm.detect import DrmInfo, KeySystem, describe_keys
from app.drm.settings import DrmSettings
from app.hls.playlist import MasterPlaylist, is_master, parse_master, parse_media
from app.net.fetcher import Fetcher

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/drm", tags=["drm"])

# A Widevine licence is a few kilobytes and a challenge smaller still. The cap is what stops
# the relay being used as a general-purpose proxy for something else.
MAX_CHALLENGE_BYTES = 256 * 1024
LICENSE_TIMEOUT_S = 30.0

# Starting a session waits on this, so it is bounded tightly: the playlist it reads is the one
# the analysis is about to fetch anyway, from the same host, and a channel whose master takes
# longer than this to answer has a finding coming regardless.
PROBE_TIMEOUT_S = 4.0


@router.get("/settings")
async def read_drm_settings() -> dict[str, object]:
    stored = await drm_settings.load()
    return {"drm": stored.describe(), "endpoint_default": drm_settings.DEFAULTS.cpix_endpoint}


@router.put("/settings")
async def write_drm_settings(payload: dict[str, object]) -> dict[str, object]:
    """Store the DRM configuration. A job already running keeps what it started with.

    A field left out is left alone, so a page that never receives the credentials back cannot
    blank them by saving the rest of the form.
    """
    current = await drm_settings.load()
    merged = {**current.model_dump(), **{k: v for k, v in payload.items() if v is not None}}
    try:
        updated = DrmSettings(**merged)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{exc}") from exc
    await drm_settings.save(updated)
    return {"drm": updated.describe()}


async def read_protection(url: str, *, timeout_s: float = PROBE_TIMEOUT_S) -> DrmInfo:
    """What one playback URL is protected with.

    The playlist is fetched by the backend, like the channel catalogue and for the same
    reason: a packager sends no CORS headers, so a page cannot read its own playback URL to
    find out whether it is protected. A URL that does not answer comes back as unknown rather
    than as clear, so a protected channel is never reported as one that needs no licence.
    """
    fetcher = Fetcher(timeout_s=timeout_s)
    try:
        result = await fetcher.fetch(url)
        if not result.ok:
            return DrmInfo(
                reason=f"The playback URL answered HTTP {result.status}."
                if result.status
                else f"The playback URL did not answer: {result.error}."
            )
        text = result.text
        if not is_master(text):
            return describe_keys(parse_media(text, result.final_url).keys)
        master = parse_master(text, result.final_url)
        info = describe_keys(master.session_keys)
        if info.protected:
            return info
        # A ladder that declares nothing on the master can still declare it on each media
        # playlist, so the first rendition is read before calling the channel clear.
        return await _probe_first_rendition(fetcher, master)
    finally:
        await fetcher.aclose()


def player_config(info: DrmInfo, stored: DrmSettings) -> dict[str, object]:
    """What the player is to be configured with.

    A clear channel gets nothing and plays exactly as it always did; a Widevine channel gets
    the relay path, never the licence server's own URL.
    """
    widevine = info.system is KeySystem.WIDEVINE and stored.enabled
    return {
        "protected": info.protected,
        "system": info.system.value,
        "system_label": info.system.label,
        "key_system": "com.widevine.alpha" if widevine else "",
        "license_path": "/api/drm/license" if widevine else "",
        # False means the deployment has no licence URL configured, which is what a page says
        # instead of letting the player fail with an EME error nobody can read.
        "configured": stored.playback_configured if widevine else True,
    }


@router.get("/probe")
async def probe(url: str = Query(..., min_length=1)) -> dict[str, object]:
    """What one playback URL needs, for a tab that has a URL but has not started a run."""
    stored = await drm_settings.load()
    info = await read_protection(url)
    return {
        **info.as_dict(),
        "player": player_config(info, stored),
        "keys_configured": stored.keys_configured,
    }


async def _probe_first_rendition(fetcher: Fetcher, master: MasterPlaylist) -> DrmInfo:
    """What the first rendition of a ladder declares, where the master declares nothing."""
    if not master.variants:
        return DrmInfo()
    result = await fetcher.fetch(master.variants[0].resolved_uri)
    if not result.ok:
        return DrmInfo()
    return describe_keys(parse_media(result.text, result.final_url).keys)


@router.post("/license")
async def acquire_license(request: Request) -> Response:
    """Relay one Widevine licence challenge to the licence server and return the licence.

    The body in and the body out are both opaque bytes; nothing here reads, stores or logs
    either. What is logged is the status and the size, which is what an operator needs when
    playback fails.
    """
    stored = await drm_settings.load()
    if not stored.enabled:
        raise HTTPException(status_code=409, detail="DRM is switched off in Settings → DRM.")
    license_url = stored.license_url.strip()
    if not license_url:
        raise HTTPException(
            status_code=409,
            detail="No licence URL is configured. Set it in Settings → DRM.",
        )

    challenge = await request.body()
    if not challenge:
        raise HTTPException(status_code=400, detail="The licence challenge body is empty.")
    if len(challenge) > MAX_CHALLENGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"The licence challenge is {len(challenge)} bytes, past the "
                f"{MAX_CHALLENGE_BYTES}-byte ceiling this relay accepts."
            ),
        )

    fetcher = Fetcher(timeout_s=LICENSE_TIMEOUT_S)
    try:
        result = await fetcher.fetch(
            license_url,
            method="POST",
            content=challenge,
            headers={"Content-Type": "application/octet-stream"},
        )
    finally:
        await fetcher.aclose()

    logger.info(
        "licence request relayed: HTTP %s, %s byte challenge, %s byte answer",
        result.status,
        len(challenge),
        result.bytes_received,
    )
    if not result.ok:
        raise HTTPException(
            status_code=502,
            detail=f"The licence server answered HTTP {result.status}."
            if result.status
            else f"The licence server did not answer: {result.error}.",
        )
    return Response(content=result.body, media_type="application/octet-stream")

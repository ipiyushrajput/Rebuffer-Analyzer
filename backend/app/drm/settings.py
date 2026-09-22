"""What this deployment is configured with for DRM, and where those values come from.

The analyzer is configured once, by whoever sets the deployment up, and after that a DRM
channel is analysed exactly like a clear one: an analyst pastes a playback URL into Realtime,
Aging, Bulk or a batch and fills in nothing else. Detection, the key request, decryption and
licence acquisition all happen on the backend.

**No key material is in this repository and none is written to it.** The three CPIX PEM
sources are a filesystem path or an HTTPS URL, read at run time; `backend/.env.example`
carries their names with empty values, like every other credential in this project. The
values are set either in `backend/.env` or in Settings → DRM, which stores them in the
settings table. Nothing here logs a key, a private key, a decrypted content key, or the
private key's whereabouts.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.config import get_settings
from app.db import session as db_session
from app.db.models import SettingRow
from app.drm.cpix import DEFAULT_ENDPOINT, CpixCredentials

SETTINGS_KEY = "drm"


class DrmSettings(BaseModel):
    """The DRM block. Editable in Settings → DRM and stored in the database."""

    # Whether protected channels are analysed at all. Off means a DRM channel is still
    # measured for transport and timing and says why its payload was not read, which is what
    # a deployment with no route to the key server wants.
    enabled: bool = Field(default=True)

    # The CPIX credentials. Each is a filesystem path on the analyzer host or an HTTPS URL.
    cpix_endpoint: str = Field(default=DEFAULT_ENDPOINT)
    cpix_client_cert: str = Field(default="")
    cpix_client_key: str = Field(default="")
    cpix_server_cert: str = Field(default="")
    # What a CPIX document names the content as. One value for the deployment; the key server
    # keys on the identifier, not on this.
    cpix_content_id: str = Field(default="rba")

    # The Widevine licence server the player acquires a licence from. The browser never talks
    # to it directly — `POST /api/drm/license` relays the challenge — so a licence server that
    # sends no CORS headers still plays.
    license_url: str = Field(default="")

    # Whether an evidence bundle carries the decrypted media as well as the encrypted bytes.
    # Off by default: decrypted media is the content in the clear, and a bundle is forwarded
    # to whoever a defect belongs to. On, the bundle adds `decrypted/` beside `encrypted/`,
    # which is what a packager needs to reproduce a bitstream defect. The key itself is never
    # written, decrypted or not.
    decrypt_evidence: bool = Field(default=False)

    @property
    def credentials(self) -> CpixCredentials:
        return CpixCredentials(
            client_cert=self.cpix_client_cert.strip(),
            client_key=self.cpix_client_key.strip(),
            server_cert=self.cpix_server_cert.strip(),
            endpoint=self.cpix_endpoint.strip() or DEFAULT_ENDPOINT,
        )

    @property
    def keys_configured(self) -> bool:
        return self.enabled and self.credentials.configured

    @property
    def playback_configured(self) -> bool:
        return self.enabled and bool(self.license_url.strip())

    def describe(self) -> dict[str, Any]:
        """What a page and a report may know.

        The licence URL is shown because the player needs it and the operator pasted it. The
        three PEM sources are reported only as set or not set: a path can carry a hostname
        worth keeping off a page, and the private key's whereabouts is not a thing to publish.
        """
        return {
            "enabled": self.enabled,
            "license_url": self.license_url,
            "cpix_content_id": self.cpix_content_id,
            "keys_configured": self.keys_configured,
            "playback_configured": self.playback_configured,
            "decrypt_evidence": self.decrypt_evidence,
            **self.credentials.describe(),
        }


DEFAULTS = DrmSettings()


def from_environment() -> DrmSettings:
    """The values `backend/.env` carries, which are the defaults until Settings overrides."""
    settings = get_settings()
    return DrmSettings(
        enabled=settings.rba_drm_enabled,
        cpix_endpoint=settings.cpix_endpoint or DEFAULT_ENDPOINT,
        cpix_client_cert=settings.cpix_client_cert,
        cpix_client_key=settings.cpix_client_key,
        cpix_server_cert=settings.cpix_server_cert,
        cpix_content_id=settings.cpix_content_id or "rba",
        license_url=settings.drm_license_url,
    )


async def load() -> DrmSettings:
    """The stored DRM settings, falling back to the environment and then the defaults.

    A settings table that does not answer must not stop an analysis: the run then uses what
    the environment carries and reports, per rendition, why a payload was not read.
    """
    base = from_environment()
    try:
        async with db_session.session_scope() as session:
            row = await session.get(SettingRow, SETTINGS_KEY)
            if row is not None and isinstance(row.value, dict):
                return DrmSettings(**{**base.model_dump(), **row.value})
    except Exception:
        return base
    return base


async def save(settings: DrmSettings) -> DrmSettings:
    """Store the DRM settings. A job already running keeps the ones it started with."""
    async with db_session.session_scope() as session:
        row = await session.get(SettingRow, SETTINGS_KEY)
        if row is None:
            session.add(SettingRow(key=SETTINGS_KEY, value=settings.model_dump()))
        else:
            row.value = settings.model_dump()
    return settings

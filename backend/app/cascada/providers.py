"""Which provider CASCADA files each channel under.

The historical API names a channel by `provider_name`, `channel_name` and `channel_id`, and the
TV Plus catalogue carries none of the first two. CASCADA's own channel-group list does:

    GET /api/channelgroup/v1?meta_join=true&account_based=true&only_on_service=false
                             &with_vod=true&is_main_channel_name=true

What a real response looks like (≈20 MB, 78 195 entries, read on 2026-09-23):

* `{"channel_list": [...], "model_list": {...}}`. Every entry is flat — `channel_group_name`,
  `provider_name`, `channel_name`, `channel_id`, `channel_country`, `on_service`,
  `is_allow_to_viewer`, `is_user_changable` — with no nesting. The group name is a field of
  the entry, and one channel appears once per group it belongs to: "Master Group", "Master
  Group - Local Channel Included", "Area Group - …", "Country Group - …", "Provider Group - …".
* The same channel is listed again for every past name and provider, switched off:
  `USBA3000041ZP` carries "Movie Hub" (on) and "The Movie Hub" (off) in each group. So the
  group **and** `on_service is True` are both needed; either alone leaves duplicates.
* After both, 5 063 channel ids map to exactly one provider and 13 to two — every one of the
  13 is "SMTOWN", filed under both `NASB_Engineering` and `NEWID`.
* A handful of `channel_name` values carry raw newlines, which strict JSON refuses, so the body
  is parsed with `strict=False` and the name is passed on exactly as CASCADA wrote it.

The join is on `channel_id` == the catalogue's service id, never on a name. CASCADA's
`channel_name` for that id is what the historical call is made with, because the catalogue's
name for the same channel can differ; a difference is logged.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode

from app.cascada.auth import CascadaAuth, CascadaAuthError
from app.cascada.client import CHANNEL_GROUP_NAME, CascadaError, _auth_failure
from app.config import get_settings, get_thresholds
from app.net.fetcher import Fetcher

logger = logging.getLogger(__name__)

API_PATH = "/api/channelgroup/v1"
QUERY = (
    ("meta_join", "true"),
    ("account_based", "true"),
    ("only_on_service", "false"),
    ("with_vod", "true"),
    ("is_main_channel_name", "true"),
)

# The body is about 20 MB; the realtime timeout is sized for one channel's week.
MIN_TIMEOUT_S = 180.0

Status = Literal["found", "ambiguous", "not_found"]


@dataclass(frozen=True, slots=True)
class Provider:
    """One provider CASCADA files a channel under, and CASCADA's name for the channel there."""

    provider_name: str
    channel_name: str
    channel_country: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "provider_name": self.provider_name,
            "channel_name": self.channel_name,
            "channel_country": self.channel_country,
        }


@dataclass(frozen=True, slots=True)
class Resolution:
    """What the map says about one catalogue channel."""

    channel_id: str
    status: Status
    # The provider the historical call is made with; None when none was found.
    chosen: Provider | None
    # Every provider left after filtering, so an ambiguity can be listed in full.
    candidates: tuple[Provider, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "status": self.status,
            "chosen": self.chosen.as_dict() if self.chosen else None,
            "candidates": [c.as_dict() for c in self.candidates],
        }


@dataclass(slots=True)
class ProviderMap:
    """`channel_id` → every provider left after the group and `on_service` filter."""

    entries: dict[str, tuple[Provider, ...]]
    fetched_at: dt.datetime
    rows_read: int = 0
    rows_kept: int = 0
    group: str = CHANNEL_GROUP_NAME
    ambiguous_ids: tuple[str, ...] = field(default_factory=tuple)

    def resolve(self, channel_id: str) -> Resolution:
        """The provider for one channel, chosen the same way every time.

        Several providers after filtering: the first by provider name, then channel name.
        That is arbitrary but stable, so two scans of the same channel send the same payload,
        and every candidate is reported so the choice is visible.
        """
        found = self.entries.get(channel_id.strip(), ())
        if not found:
            return Resolution(channel_id=channel_id, status="not_found", chosen=None)
        if len(found) == 1:
            return Resolution(
                channel_id=channel_id, status="found", chosen=found[0], candidates=found
            )
        return Resolution(
            channel_id=channel_id, status="ambiguous", chosen=found[0], candidates=found
        )

    def describe(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at.isoformat(),
            "group": self.group,
            "rows_read": self.rows_read,
            "rows_kept": self.rows_kept,
            "channels": len(self.entries),
            "ambiguous_channels": len(self.ambiguous_ids),
        }


def build_map(payload: Any, *, group: str = CHANNEL_GROUP_NAME) -> ProviderMap:
    """The map from one channel-group response."""
    rows = payload.get("channel_list") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        keys = ", ".join(sorted(str(k) for k in payload)) if isinstance(payload, dict) else ""
        raise CascadaError(
            "The CASCADA channel-group response carries no `channel_list`. "
            f"Keys present: {keys or 'none'}."
        )

    grouped: dict[str, dict[str, Provider]] = {}
    kept = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("channel_group_name") != group or row.get("on_service") is not True:
            continue
        channel_id = str(row.get("channel_id") or "").strip()
        provider = str(row.get("provider_name") or "").strip()
        if not channel_id or not provider:
            continue
        kept += 1
        # One entry per provider: a provider listed twice for the same id is one candidate.
        grouped.setdefault(channel_id, {}).setdefault(
            provider,
            Provider(
                provider_name=provider,
                channel_name=str(row.get("channel_name") or ""),
                channel_country=str(row.get("channel_country") or ""),
            ),
        )

    entries = {
        channel_id: tuple(
            sorted(providers.values(), key=lambda p: (p.provider_name, p.channel_name))
        )
        for channel_id, providers in grouped.items()
    }
    return ProviderMap(
        entries=entries,
        fetched_at=dt.datetime.now(dt.UTC),
        rows_read=len(rows),
        rows_kept=kept,
        group=group,
        ambiguous_ids=tuple(sorted(cid for cid, found in entries.items() if len(found) > 1)),
    )


def parse_body(body: str) -> ProviderMap:
    try:
        # CASCADA writes a few channel names with raw newlines in them.
        payload = json.loads(body, strict=False)
    except ValueError as exc:
        raise CascadaError(
            f"The CASCADA channel-group list ({len(body)} bytes) does not parse as JSON: {exc}."
        ) from exc
    return build_map(payload)


def build_url(base_url: str | None = None) -> str:
    base = (base_url or get_settings().cascada_base_url).rstrip("/")
    return f"{base}{API_PATH}?{urlencode(QUERY)}"


async def fetch_map(auth: CascadaAuth, fetcher: Fetcher | None = None) -> ProviderMap:
    """Read the channel-group list from CASCADA and build the map."""
    settings = get_settings()
    own = fetcher is None
    client = fetcher or Fetcher(
        timeout_s=max(settings.cascada_timeout_s, MIN_TIMEOUT_S),
        per_host_connections=settings.rba_per_host_connections,
    )
    timeout = max(settings.cascada_timeout_s, MIN_TIMEOUT_S)
    try:
        result = await client.fetch(build_url(), headers=auth.headers(), timeout_s=timeout)
    finally:
        if own:
            await client.aclose()

    reason = _auth_failure(result.status, result.final_url)
    if reason is not None:
        raise CascadaAuthError(
            f"{reason} Paste a fresh session in the Settings tab, then run this again."
        )
    if result.error is not None:
        raise CascadaError(f"The CASCADA channel-group list failed on the wire: {result.error}.")
    if not result.ok:
        raise CascadaError(f"CASCADA answered HTTP {result.status} for the channel-group list.")
    found = parse_body(result.text)
    logger.info(
        "CASCADA provider map: %d channel(s) from %d row(s), %d with more than one provider",
        len(found.entries),
        found.rows_read,
        len(found.ambiguous_ids),
    )
    return found


# The map for this process. It is a cache of a CASCADA list, rebuilt when it is older than
# `cascada_provider_map_ttl_hours` or when an operator asks, and never the only copy of anything.
_cached: ProviderMap | None = None
_lock = asyncio.Lock()


def cached() -> ProviderMap | None:
    return _cached


def _fresh(entry: ProviderMap | None) -> bool:
    if entry is None:
        return False
    ttl = dt.timedelta(hours=max(0, get_thresholds().cascada_provider_map_ttl_hours))
    return dt.datetime.now(dt.UTC) - entry.fetched_at <= ttl


async def provider_map(
    auth: CascadaAuth, *, refresh: bool = False, fetcher: Fetcher | None = None
) -> ProviderMap:
    """The map, from the cache while it is fresh, else from CASCADA — once, however many ask."""
    global _cached
    if not refresh and _fresh(_cached):
        assert _cached is not None
        return _cached
    async with _lock:
        if not refresh and _fresh(_cached):
            assert _cached is not None
            return _cached
        _cached = await fetch_map(auth, fetcher)
        return _cached


def set_cached(entry: ProviderMap | None) -> None:
    """Replace the cached map; tests and the refresh endpoint use it."""
    global _cached
    _cached = entry

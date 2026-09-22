"""WebSocket message schemas (§3.1).

The same shape is declared on both sides: the TypeScript mirror lives in
`frontend/src/ws/messages.ts` and is kept in step with this file.

    { "type": "status|playlist_snapshot|segment_result|metric|player_event|finding|verdict",
      "session_id": "…", "ts": "ISO-8601", "layer": "PLAYBACK|ORIGIN|CDN|SSAI",
      "variant_id": "v720p|audio_en|…", "data": { } }
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field

ServerMessageType = Literal[
    "status",
    "playlist_snapshot",
    "segment_result",
    "metric",
    "player_event",
    "finding",
    "verdict",
    "event",
    "error",
]

ClientMessageType = Literal["player_event", "ping", "subscribe"]


class ServerMessage(BaseModel):
    type: ServerMessageType
    session_id: str
    ts: str = Field(default_factory=lambda: dt.datetime.now(dt.UTC).isoformat())
    layer: str | None = None
    variant_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class PlayerEvent(BaseModel):
    """Telemetry the browser sends back over the same socket."""

    event: str
    ts: str | float | None = None
    variant: str | None = None
    level: int | None = None
    # The rendition's own bitrate, sent only on a rung switch.
    bitrate: int | None = None
    # What the player measured the network doing, on every fragment load. A different
    # quantity from `bitrate` and kept in its own field: hls.js estimates in gigabits per
    # second on a fast local fetch, which is not a rung anyone is playing.
    bandwidth_bps: int | None = None
    buffer_s: float | None = None
    stall_duration_s: float | None = None
    dropped: int | None = None
    total_frames: int | None = None
    error_type: str | None = None
    details: str | None = None
    fatal: bool | None = None
    startup_ms: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ClientMessage(BaseModel):
    type: ClientMessageType
    data: dict[str, Any] = Field(default_factory=dict)


def server_message(
    kind: str,
    session_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Build the envelope from an engine event."""
    data = payload.get("data", {})
    if not isinstance(data, dict):
        data = {"value": data}
    extras = {
        key: value
        for key, value in payload.items()
        if key not in ("type", "ts", "data", "layer", "variant")
    }
    if extras:
        data = {**data, **extras}
    return ServerMessage(
        type=kind,  # type: ignore[arg-type]
        session_id=session_id,
        ts=str(payload.get("ts") or dt.datetime.now(dt.UTC).isoformat()),
        layer=payload.get("layer"),
        variant_id=payload.get("variant") or data.get("variant"),
        data=data,
    ).model_dump()

"""WebSocket endpoints.

`/ws/realtime/{id}` is bidirectional: analysis events flow to the client, and the client
sends hls.js telemetry back on the same socket so player stalls are correlated against the
backend events measured at the same instant.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.jobs.manager import job_manager
from app.ws.hub import hub

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/realtime/{session_id}")
async def realtime_socket(websocket: WebSocket, session_id: str) -> None:
    await hub.connect(session_id, websocket)
    handle = job_manager.get(session_id)
    if handle is not None:
        await websocket.send_json(
            {
                "type": "status",
                "session_id": session_id,
                "ts": handle.created_at.isoformat(),
                "data": handle.summary(),
            }
        )
    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "ping":
                await websocket.send_json(
                    {"type": "status", "session_id": session_id, "data": {"pong": True}}
                )
            elif kind == "player_event":
                await job_manager.ingest_player_event(session_id, message.get("data", {}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.info("realtime socket for %s closed: %s", session_id, type(exc).__name__)
    finally:
        await hub.disconnect(session_id, websocket)


@router.websocket("/ws/jobs/{job_id}")
async def job_socket(websocket: WebSocket, job_id: str) -> None:
    """Read-only progress stream for an aging or bulk job."""
    await hub.connect(job_id, websocket)
    handle = job_manager.get(job_id)
    if handle is not None:
        await websocket.send_json(
            {"type": "status", "session_id": job_id, "data": handle.summary()}
        )
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.info("job socket for %s closed: %s", job_id, type(exc).__name__)
    finally:
        await hub.disconnect(job_id, websocket)

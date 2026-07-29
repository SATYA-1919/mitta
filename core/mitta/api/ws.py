"""WebSocket endpoint — Channel A (API_DESIGN.md §4).

Turns stream here. The socket carries frames in the envelope every client
already understands; `turn.start` opens a turn and the orchestrator's events are
forwarded verbatim.

Authentication is by subprotocol header, never a query parameter (DEC-026): a
token in a URL lands in every access log and proxy trace it passes through.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from mitta.agent.orchestrator import Orchestrator
from mitta.api.auth import SUBPROTOCOL, authenticate_websocket
from mitta.conversations.models import InputKind
from mitta.ids import MESSAGE, prefixed
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

router = APIRouter()


def _frame(frame_type: str, data: dict[str, Any], ref: str | None = None) -> str:
    envelope: dict[str, Any] = {
        "id": prefixed(MESSAGE),
        "type": frame_type,
        "ts": _now_iso(),
        "data": data,
    }
    if ref is not None:
        envelope["ref"] = ref
    return json.dumps(envelope)


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@router.websocket("/v1/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    orchestrator: Orchestrator | None = websocket.app.state.orchestrator

    # Rejects and closes with 4401/4403 before `accept`, so an unauthenticated
    # peer never holds an open socket.
    if not await authenticate_websocket(websocket):
        return

    # Echoing the subprotocol is required: a browser that offered one and gets
    # no confirmation fails the handshake on the client side.
    await websocket.accept(subprotocol=SUBPROTOCOL)
    log.info("ws.connected")

    try:
        while True:
            raw = await websocket.receive_text()
            await _handle(websocket, orchestrator, raw)
    except WebSocketDisconnect:
        log.info("ws.disconnected")
    except Exception:
        log.exception("ws.failed")
        await websocket.close(code=1011)


async def _handle(websocket: WebSocket, orchestrator: Orchestrator | None, raw: str) -> None:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.send_text(
            _frame("error", {"code": "validation.failed", "message": "Malformed frame"})
        )
        return

    frame_type = message.get("type")
    data = message.get("data") or {}

    if frame_type == "subscribe":
        # Accepted and acknowledged. Channel filtering is a client concern for
        # now; the server sends only what the client's own turns produce.
        await websocket.send_text(_frame("subscribed", {"channels": data.get("channels", [])}))
        return

    if frame_type == "ping":
        await websocket.send_text(_frame("pong", {}))
        return

    if frame_type == "turn.start":
        await _run_turn(websocket, orchestrator, data)
        return

    if frame_type == "resume":
        # Frames are not buffered yet, so resume is acknowledged rather than
        # honoured. Saying so beats silence, which the client would read as a
        # successful resume and then wonder about the gap.
        await websocket.send_text(
            _frame("resume.unsupported", {"detail": "frame replay is not implemented"})
        )
        return

    await websocket.send_text(
        _frame("error", {"code": "validation.failed", "message": f"Unknown frame: {frame_type}"})
    )


async def _run_turn(
    websocket: WebSocket, orchestrator: Orchestrator | None, data: dict[str, Any]
) -> None:
    if orchestrator is None:
        await websocket.send_text(
            _frame(
                "turn.error",
                {
                    "code": "agent.unavailable",
                    "message": "The agent is not available.",
                    "retryable": False,
                },
            )
        )
        return

    text = str(data.get("text") or "").strip()
    if not text:
        await websocket.send_text(
            _frame("turn.error", {"code": "validation.failed", "message": "Empty message"})
        )
        return

    conversation_id = data.get("conversation_id")
    kind = data.get("input_kind", InputKind.TEXT.value)
    try:
        input_kind = InputKind(kind)
    except ValueError:
        input_kind = InputKind.TEXT

    ref: str | None = None
    async for event in orchestrator.run(
        text=text,
        conversation_id=conversation_id if isinstance(conversation_id, str) else None,
        input_kind=input_kind,
    ):
        if event.type == "turn.accepted":
            ref = str(event.data.get("turn_id"))
        await websocket.send_text(_frame(event.type, dict(event.data), ref))
        # Yield to the event loop between frames so a fast local stream cannot
        # starve the socket's writer and arrive as one burst at the end, which
        # would defeat the point of streaming.
        await asyncio.sleep(0)

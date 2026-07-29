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
from mitta.errors import NotFoundError
from mitta.ids import MESSAGE, prefixed
from mitta.policy.broker import ApprovalBroker, ApprovalOutcome
from mitta.policy.engine import PolicyEngine
from mitta.telemetry.logging import get_logger
from mitta.tools.base import ToolSpec
from mitta.tools.registry import ToolRegistry

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

    # Turns run as tasks, not inline.
    #
    # A turn can pause waiting for the user to approve a tool. Awaiting it here
    # would stop this loop calling `receive_text`, so the approval the turn is
    # waiting for could never be read — the connection deadlocked until the
    # keepalive ping timed out, which is exactly how it was found.
    running: set[asyncio.Task[None]] = set()

    try:
        while True:
            raw = await websocket.receive_text()
            task = asyncio.create_task(_guarded(websocket, _handle(websocket, orchestrator, raw)))
            # Held so the task is not garbage-collected mid-flight, which
            # asyncio permits and which cancels the turn silently.
            running.add(task)
            task.add_done_callback(running.discard)
    except WebSocketDisconnect:
        log.info("ws.disconnected")
    except Exception:
        log.exception("ws.failed")
        await websocket.close(code=1011)
    finally:
        # A closed socket cannot deliver an approval, so anything still waiting
        # would sit until its own timeout for no reason.
        for task in running:
            task.cancel()


async def _handle(websocket: WebSocket, orchestrator: Orchestrator | None, raw: str) -> None:
    """Dispatch one frame. Runs as its own task; must not raise."""
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

    if frame_type in ("turn.approve", "turn.deny"):
        await _resolve_approval(websocket, frame_type == "turn.approve", data)
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


async def _guarded(websocket: WebSocket, coro: Any) -> None:
    """Run a handler without letting a failure close the socket.

    Each frame is now its own task, so an unhandled exception would be an
    orphaned traceback rather than something the connection loop could report.
    """
    try:
        await coro
    except Exception:
        log.exception("ws.frame_failed")


async def _resolve_approval(websocket: WebSocket, approved: bool, data: dict[str, Any]) -> None:
    """Deliver the user's decision to the turn waiting on it."""
    broker: ApprovalBroker | None = websocket.app.state.approval_broker
    policy: PolicyEngine | None = websocket.app.state.policy
    request_id = str(data.get("request_id") or "")

    if broker is None or policy is None or not request_id:
        return

    pending = next((r for r in broker.pending() if r.id == request_id), None)
    if pending is None:
        # A stale id is unremarkable: a duplicate click, or a prompt that timed
        # out while the user was reading it. Acknowledged rather than errored.
        await websocket.send_text(_frame("turn.approval_stale", {"request_id": request_id}))
        return

    spec = _spec_for(websocket, pending.tool_name)
    if spec is None:
        return

    if approved:
        # The token is minted here, from the parameters recorded when the
        # prompt was raised — not from anything the client sent back. A client
        # that returned altered parameters would get a token that fails
        # verification against the arguments actually used.
        token = policy.request_approval(spec, pending.params, turn_id=pending.turn_id)
        broker.resolve(request_id, ApprovalOutcome(True, "approved", token))
    else:
        policy.deny(spec, pending.params, turn_id=pending.turn_id)
        broker.resolve(request_id, ApprovalOutcome(False, "denied by the user"))


def _spec_for(websocket: WebSocket, tool_name: str) -> ToolSpec | None:
    registry: ToolRegistry | None = websocket.app.state.tool_registry
    if registry is None:
        return None
    try:
        return registry.get(tool_name).spec
    except NotFoundError:
        return None


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

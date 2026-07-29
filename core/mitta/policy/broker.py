"""Waiting for a human.

A turn that needs approval has to stop mid-flight and wait for someone to click
something. This is what it waits on.

**Why an in-flight pause rather than ending the turn.** `turns.status` has an
`awaiting_approval` value, and the alternative design — end the turn, resume it
later from a new frame — is the more durable one: it survives a restart. It also
means re-running everything before the tool call, including the model pass that
chose it, so the user pays for their own hesitation and may get a different plan
than the one they approved.

Holding the turn open trades durability for that. The cost is bounded: a pending
request times out, and a process that dies mid-approval loses a turn the user
was already watching fail. The `awaiting_approval` status remains in the schema
for the durable version, which the planner will need for multi-step work that
genuinely cannot be re-run.

**A timeout is not optional.** Without one, a user who closes the window leaves a
turn holding a socket forever, and MITTA looks like it is thinking about
something it will never finish. Timing out is treated as a denial — the safe
direction, since the alternative is acting on silence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from mitta.ids import REQUEST, prefixed
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

#: How long a turn waits. Long enough to read a prompt and decide; short enough
#: that an abandoned window does not leave work hanging.
DEFAULT_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    id: str
    tool_name: str
    params: dict[str, Any]
    prompt: str
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    approved: bool
    reason: str
    #: Present only on approval — the signed token the executor then verifies.
    token: dict[str, Any] | None = None


@dataclass
class _Pending:
    request: ApprovalRequest
    future: asyncio.Future[ApprovalOutcome] = field(
        default_factory=lambda: asyncio.get_event_loop().create_future()
    )


class ApprovalBroker:
    """Pairs a paused turn with the user's answer.

    Not thread-safe by design: everything here runs on the API event loop, and
    a lock would imply a concurrency this does not have. Cross-thread access
    would be a bug worth failing loudly on rather than accommodating.
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._pending: dict[str, _Pending] = {}
        self._timeout = timeout

    def open(
        self,
        *,
        tool_name: str,
        params: dict[str, Any],
        prompt: str,
        turn_id: str | None = None,
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            id=prefixed(REQUEST),
            tool_name=tool_name,
            params=params,
            prompt=prompt,
            turn_id=turn_id,
        )
        self._pending[request.id] = _Pending(request=request)
        log.info(
            "approval.requested",
            extra={"request_id": request.id, "tool": tool_name, "turn_id": turn_id},
        )
        return request

    async def wait(self, request_id: str) -> ApprovalOutcome:
        """Block until the user answers, or the timeout fires."""
        pending = self._pending.get(request_id)
        if pending is None:
            return ApprovalOutcome(False, "no such approval request")

        try:
            return await asyncio.wait_for(pending.future, timeout=self._timeout)
        except TimeoutError:
            # Silence is not consent.
            log.info("approval.timed_out", extra={"request_id": request_id})
            return ApprovalOutcome(False, "timed out waiting for approval")
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, outcome: ApprovalOutcome) -> bool:
        """Deliver an answer. Returns False if nothing was waiting.

        A stale id is common and unremarkable — a user clicking Approve on a
        prompt that has already timed out, or a duplicate click. It is reported
        rather than raised so the socket handler does not have to distinguish.
        """
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(outcome)
        return True

    def pending(self) -> list[ApprovalRequest]:
        return [entry.request for entry in self._pending.values()]

    def cancel_all(self, reason: str = "cancelled") -> None:
        """Release every waiter. Used on shutdown.

        Without this a turn awaiting approval would keep the event loop alive
        past the point the process was asked to stop.
        """
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(ApprovalOutcome(False, reason))
        self._pending.clear()

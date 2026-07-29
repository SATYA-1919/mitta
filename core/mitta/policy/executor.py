"""Policy-gated tool execution.

Lives in `mitta.policy`, not `mitta.tools`, and the placement is the design
rather than filing. `policy.engine` must read `ToolSpec` and `Risk` to reach a
verdict, so policy sits above tools in the layer order; an executor under
`tools` that reached back up for `PolicyEngine` would close that into a cycle.

More to the point, enforcement is what this does. It is not a tool, and putting
it beside the tools would suggest a tool could construct one.

The single path from "the model asked for a tool" to "something happened". Every
call goes through `PolicyEngine.authorise` first and is recorded in
`tool_invocations` regardless of outcome — including the calls that were denied,
which are the ones a user most wants to be able to look up later.

Nothing here reaches the platform. Tools do the work; this decides whether they
may, and writes down what occurred.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from mitta.errors import ApprovalInvalidError, MittaError, NotFoundError
from mitta.ids import INVOCATION, prefixed
from mitta.persistence.database import Database
from mitta.policy.approval import hash_params
from mitta.policy.engine import PolicyEngine
from mitta.telemetry.logging import get_logger
from mitta.tools.base import ToolResult, ToolSpec
from mitta.tools.registry import ToolRegistry

log = get_logger(__name__)

#: Tool output is fed back into the next request's context. A tool returning a
#: megabyte would blow the budget R5's chokepoint exists to enforce, so the
#: result is truncated here rather than trusted to be small.
MAX_RESULT_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class Execution:
    invocation_id: str
    tool_name: str
    params: dict[str, Any]
    result: ToolResult
    #: Set when the tool needs approval the caller has not supplied. The turn
    #: pauses; it does not fail.
    awaiting_approval: bool = False
    prompt: str | None = None


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, policy: PolicyEngine, db: Database) -> None:
        self._registry = registry
        self._policy = policy
        self._db = db

    async def execute(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        turn_id: str | None = None,
        approval_id: str | None = None,
        signature: str | None = None,
    ) -> Execution:
        """Run one tool call. Never raises for an expected failure.

        A tool that errors, is denied, or does not exist all produce a result
        the model can read and respond to. Raising would abort a turn that is
        otherwise going fine, and "I could not do that because X" is a better
        answer than a stack trace.
        """
        try:
            tool = self._registry.get(tool_name)
        except NotFoundError:
            # A model inventing a tool name is routine, not exceptional.
            return self._record(
                tool_name,
                params,
                ToolResult.failure(f"No such tool: {tool_name}"),
                verdict="deny",
                turn_id=turn_id,
            )

        spec = tool.spec

        try:
            decision = self._policy.authorise(
                spec,
                params,
                approval_id=approval_id,
                signature=signature,
                turn_id=turn_id,
            )
        except ApprovalInvalidError as exc:
            return self._record(
                tool_name, params, ToolResult.failure(exc.message), verdict="deny", turn_id=turn_id
            )

        if not decision.allowed:
            # Not a failure. The turn pauses for the user to answer.
            execution = self._record(
                tool_name,
                params,
                ToolResult.failure("Waiting for your approval."),
                verdict="confirm",
                turn_id=turn_id,
                status="pending",
            )
            return Execution(
                invocation_id=execution.invocation_id,
                tool_name=tool_name,
                params=params,
                result=execution.result,
                awaiting_approval=True,
                prompt=decision.prompt,
            )

        started = time.monotonic()
        try:
            result = await tool.run(params)
        except MittaError as exc:
            result = ToolResult.failure(exc.message)
        except Exception as exc:
            log.exception("tool.crashed", extra={"tool": tool_name})
            result = ToolResult.failure(f"{tool_name} failed unexpectedly: {exc}")

        result = _truncate(result)
        self._policy.record_result(
            spec,
            params,
            succeeded=result.ok,
            turn_id=turn_id,
            detail={"duration_ms": int((time.monotonic() - started) * 1000)},
        )
        return self._record(
            tool_name,
            params,
            result,
            verdict="allow",
            turn_id=turn_id,
            status="succeeded" if result.ok else "failed",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _record(
        self,
        tool_name: str,
        params: dict[str, Any],
        result: ToolResult,
        *,
        verdict: str,
        turn_id: str | None,
        status: str = "denied",
        duration_ms: int | None = None,
    ) -> Execution:
        invocation_id = prefixed(INVOCATION)
        with self._db.write() as conn:
            conn.execute(
                """
                INSERT INTO tool_invocations
                    (id, turn_id, tool_name, params, params_hash, verdict,
                     status, result, duration_ms, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    invocation_id,
                    turn_id,
                    tool_name,
                    json.dumps(params, sort_keys=True),
                    hash_params(tool_name, params),
                    verdict,
                    status,
                    json.dumps({"ok": result.ok, "content": result.content[:1000]}),
                    duration_ms,
                    int(time.time()),
                ),
            )
        return Execution(
            invocation_id=invocation_id,
            tool_name=tool_name,
            params=params,
            result=result,
        )

    def specs(self) -> list[ToolSpec]:
        return self._registry.specs()


def _truncate(result: ToolResult) -> ToolResult:
    if len(result.content) <= MAX_RESULT_CHARS:
        return result
    # Says it was cut. A silently truncated result reads to the model as a
    # complete one, and it will answer confidently from half the evidence.
    return ToolResult(
        ok=result.ok,
        content=result.content[:MAX_RESULT_CHARS] + "\n\n[truncated]",
        detail=result.detail,
    )

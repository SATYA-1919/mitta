"""The policy engine — what MITTA is allowed to do, and when it must ask.

`ARCHITECTURE.md` §3 places this between the Tool Manager and the OS Adapter,
and the arrangement is structural rather than advisory: **the Tool Manager is
constructed without an OS Adapter reference — this holds it.** Bypassing policy
therefore requires editing the composition root, which is reviewed, and breaking
an import contract, which fails CI (DEC-029). A model cannot talk its way past a
reference that does not exist.

Three tiers, and the product owner sets them:

| Risk | Examples | Behaviour |
| --- | --- | --- |
| `READ` | web search, open an app, read an allowed file | Runs, and is **reported afterwards** |
| `WRITE` | create or edit a file, draft an email | **Asks first**, showing what changes |
| `DESTRUCTIVE` | delete, overwrite, move, outward-facing | **Asks first**, with the full list |

A read-only tool that runs silently is still an action. Web search sends the
query to a third party, which is squarely R5's concern — so `READ` tools do not
prompt, but they are always logged and always surfaced. "Did not ask" is not the
same as "did not tell".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from mitta.policy.approval import ApprovalAuthority, hash_params
from mitta.policy.audit import AuditLog, Verdict
from mitta.telemetry.logging import get_logger
from mitta.tools.base import Risk, ToolSpec

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    #: Shown to the user when confirmation is required. Written for a human, and
    #: naming the concrete effect — "delete 47 files", not "perform file
    #: operation". A prompt nobody reads is a prompt that approves everything.
    prompt: str | None = None
    reason: str = ""

    @property
    def needs_confirmation(self) -> bool:
        return self.verdict == "confirm"

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


class PolicyEngine:
    def __init__(
        self,
        audit: AuditLog,
        approvals: ApprovalAuthority,
        *,
        auto_approve_reads: bool = True,
    ) -> None:
        self._audit = audit
        self._approvals = approvals
        self._auto_approve_reads = auto_approve_reads

    def evaluate(self, spec: ToolSpec, params: dict[str, Any]) -> Decision:
        """Decide what happens before a tool runs."""
        if spec.risk is Risk.READ and self._auto_approve_reads:
            return Decision("allow", reason="read-only")

        if spec.risk is Risk.READ:
            return Decision(
                "confirm",
                prompt=spec.describe(params),
                reason="read-only, but confirmation is enabled",
            )

        return Decision(
            "confirm",
            prompt=spec.describe(params),
            reason=f"{spec.risk.value} action requires approval",
        )

    def authorise(
        self,
        spec: ToolSpec,
        params: dict[str, Any],
        *,
        approval_id: str | None = None,
        signature: str | None = None,
        turn_id: str | None = None,
    ) -> Decision:
        """Final check immediately before execution.

        Separate from `evaluate` on purpose. `evaluate` decides what to ask;
        this decides whether to proceed, and it re-derives the parameter hash
        from the arguments about to be used rather than trusting the ones the
        prompt was built from. Between the two, a plan could have changed.
        """
        decision = self.evaluate(spec, params)

        if decision.allowed:
            self._audit.record(
                actor="agent",
                action=f"tool.{spec.name}",
                subject=spec.subject(params),
                verdict="allow",
                turn_id=turn_id,
                detail={"params_hash": hash_params(spec.name, params)},
            )
            return decision

        if approval_id is None or signature is None:
            self._audit.record(
                actor="agent",
                action=f"tool.{spec.name}",
                subject=spec.subject(params),
                verdict="confirm",
                turn_id=turn_id,
                detail={"awaiting_approval": True},
            )
            return decision

        # Raises `ApprovalInvalidError` if the token is forged, expired, reused,
        # for another tool, or issued for different parameters.
        self._approvals.verify_and_consume(
            token_id=approval_id,
            signature=signature,
            tool_name=spec.name,
            params=params,
        )
        self._audit.record(
            actor="user",
            action=f"tool.{spec.name}",
            subject=spec.subject(params),
            verdict="allow",
            turn_id=turn_id,
            detail={"approval_id": approval_id},
        )
        return Decision("allow", reason="approved by the user")

    def record_result(
        self,
        spec: ToolSpec,
        params: dict[str, Any],
        *,
        succeeded: bool,
        turn_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Log what actually happened.

        Recorded even for `READ` tools that never prompted. This is the half of
        the commitment that says *told you*, as distinct from *asked you*.
        """
        self._audit.record(
            actor="agent",
            action=f"tool.{spec.name}.{'completed' if succeeded else 'failed'}",
            subject=spec.subject(params),
            turn_id=turn_id,
            detail=detail or {},
        )

    def request_approval(
        self, spec: ToolSpec, params: dict[str, Any], *, turn_id: str | None = None
    ) -> dict[str, Any]:
        """Issue a token for a user who has said yes."""
        token = self._approvals.issue(tool_name=spec.name, params=params, turn_id=turn_id)
        return token.to_wire()

    def deny(self, spec: ToolSpec, params: dict[str, Any], *, turn_id: str | None = None) -> None:
        """Record a refusal.

        "The user said no at 14:32" is exactly what an audit trail exists to
        answer, and a denial that leaves no trace is indistinguishable from
        never having asked.
        """
        self._approvals.issue(tool_name=spec.name, params=params, turn_id=turn_id, approved=False)
        self._audit.record(
            actor="user",
            action=f"tool.{spec.name}",
            subject=spec.subject(params),
            verdict="deny",
            turn_id=turn_id,
        )

    def maintenance(self, *, now: int | None = None) -> int:
        ts = now if now is not None else int(time.time())
        return self._approvals.purge_expired(now=ts)

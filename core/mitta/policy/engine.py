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

**The second axis is location.** Risk says what a tool does; the project
boundary says where. A tool that declares `path_params` has each of those
arguments resolved against the paths the user registered
(`mitta.projects.boundary`), and the result can only ever make the decision
stricter — an excluded path is refused outright, an unknown one asks. It never
makes a decision looser: `writable` widens *where* MITTA may write, not
*whether* it may write unattended, so a `WRITE` tool inside a writable root
still asks. Conflating the two would turn "this folder is in scope" into
"anything in this folder happens silently", which is not what registering a path
means.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from mitta.policy.approval import ApprovalAuthority, hash_params
from mitta.policy.audit import AuditLog, Verdict
from mitta.projects.boundary import Containment, PathBoundary, Resolution
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

    @property
    def refused(self) -> bool:
        """A standing refusal. Unlike `confirm`, no approval token can lift it."""
        return self.verdict == "deny"


#: Strictness order, used to pick the binding resolution when a tool call carries
#: several paths. A call is only as permitted as its least permitted argument —
#: taking the first match instead would let a copy from an allowed source to an
#: excluded destination through on the strength of the source.
_STRICTNESS: dict[Containment, int] = {
    Containment.WRITABLE: 0,
    Containment.READ_ONLY: 1,
    Containment.OUTSIDE: 2,
    Containment.EXCLUDED: 3,
}


class PolicyEngine:
    def __init__(
        self,
        audit: AuditLog,
        approvals: ApprovalAuthority,
        *,
        boundary: PathBoundary | None = None,
        auto_approve_reads: bool = True,
    ) -> None:
        self._audit = audit
        self._approvals = approvals
        self._boundary = boundary
        self._auto_approve_reads = auto_approve_reads

    def evaluate(self, spec: ToolSpec, params: dict[str, Any]) -> Decision:
        """Decide what happens before a tool runs.

        Location is resolved first, because an exclusion outranks every tier:
        there is no risk level at which "not this file" becomes negotiable. What
        the *rest* of the boundary means, though, depends on the tier — which is
        why `located` is a `Resolution` here and not a `Decision`. `READ_ONLY`
        forbids a write and permits a read, and a helper that had already
        collapsed that into a verdict could not say so.
        """
        located = self._locate(spec, params)
        if isinstance(located, Decision):  # A malformed path argument. Fail closed.
            return located

        if located is not None and located.containment is Containment.EXCLUDED:
            return Decision("deny", prompt=located.describe(), reason="path is excluded")

        # Only an unknown location escalates a read. Reading inside a registered
        # root — writable or not — is what registering it was for, and escalating
        # `READ_ONLY` would make adding a project mean being asked about every
        # file inside it.
        unknown = (
            located if located is not None and located.containment is Containment.OUTSIDE else None
        )

        if spec.risk is Risk.READ and self._auto_approve_reads:
            if unknown is not None:
                return Decision(
                    "confirm", prompt=unknown.describe(), reason="path is outside every project"
                )
            return Decision("allow", reason="read-only")

        if spec.risk is Risk.READ:
            return Decision(
                "confirm",
                prompt=unknown.describe() if unknown is not None else spec.describe(params),
                reason="read-only, but confirmation is enabled",
            )

        # WRITE and DESTRUCTIVE always ask, boundary or no boundary. `writable`
        # widens where MITTA may write, not whether it may do so unattended, so
        # the boundary's only contribution here is a better sentence to ask with.
        return Decision(
            "confirm",
            # The boundary's sentence when it has something the user did not
            # already know — "outside every project path you have configured" is
            # the fact that should change their answer. Otherwise the tool's own
            # description, which is more concrete about the effect.
            prompt=(
                located.describe()
                if located is not None and located.containment is not Containment.WRITABLE
                else spec.describe(params)
            ),
            reason=f"{spec.risk.value} action requires approval",
        )

    def _locate(self, spec: ToolSpec, params: dict[str, Any]) -> Resolution | Decision | None:
        """Resolve the tool's declared path arguments against the boundary.

        Returns the **strictest** resolution among them, `None` when there is
        nothing to resolve, or a refusing `Decision` when a declared path
        argument is not a usable path at all.
        """
        if self._boundary is None or not spec.path_params:
            return None

        strictest: Resolution | None = None
        for name in spec.path_params:
            if name not in params:
                continue  # An optional path argument the model did not supply.
            value = params[name]
            if not isinstance(value, str) or not value.strip():
                # Fail closed. A declared path argument that is a list, a dict or
                # a null is one the boundary cannot resolve, and carrying on
                # would hand the tool an unchecked path.
                return Decision("deny", reason=f"{name} is not a filesystem path")
            resolution = self._boundary.resolve(value)
            if (
                strictest is None
                or _STRICTNESS[resolution.containment] > _STRICTNESS[strictest.containment]
            ):
                strictest = resolution
        return strictest

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

        if decision.refused:
            # Checked before the token, and this order is the point. A refusal
            # is a standing instruction — an excluded path — not a question
            # awaiting an answer, so there is nothing an approval can consent
            # to. Verifying the token first and returning `allow` would let a
            # token issued for a path that was writable at the time survive the
            # user excluding it, which is precisely the window an exclusion
            # exists to close.
            self._audit.record(
                actor="system",
                action=f"tool.{spec.name}",
                subject=spec.subject(params),
                verdict="deny",
                turn_id=turn_id,
                detail={"reason": decision.reason, "presented_approval": approval_id is not None},
            )
            return decision

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

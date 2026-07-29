"""Approval tokens, the audit chain, and the policy engine.

These tests are the security argument. Each one names an attack that would
otherwise work.
"""

from __future__ import annotations

import pytest

from mitta.errors import ApprovalInvalidError
from mitta.persistence.database import Database
from mitta.policy.approval import ApprovalAuthority, hash_params
from mitta.policy.audit import AuditLog
from mitta.policy.engine import PolicyEngine
from mitta.tools.base import Risk, ToolResult, ToolSpec
from mitta.tools.registry import ToolRegistry

DELETE_SPEC = ToolSpec(
    name="delete_files",
    description="Delete files",
    risk=Risk.DESTRUCTIVE,
    parameters={"type": "object", "properties": {"paths": {"type": "array"}}},
)
SEARCH_SPEC = ToolSpec(name="web_search", description="Search", risk=Risk.READ)

THREE_FILES = {"paths": ["a.txt", "b.txt", "c.txt"]}


@pytest.fixture
def approvals(migrated: Database) -> ApprovalAuthority:
    return ApprovalAuthority(migrated)


@pytest.fixture
def audit(migrated: Database) -> AuditLog:
    return AuditLog(migrated)


@pytest.fixture
def policy(audit: AuditLog, approvals: ApprovalAuthority) -> PolicyEngine:
    return PolicyEngine(audit, approvals)


class TestApprovalTokens:
    def test_a_valid_token_is_accepted_once(self, approvals: ApprovalAuthority) -> None:
        token = approvals.issue(tool_name="delete_files", params=THREE_FILES)

        approvals.verify_and_consume(
            token_id=token.id,
            signature=token.signature,
            tool_name="delete_files",
            params=THREE_FILES,
        )

        # Second use is a replay.
        with pytest.raises(ApprovalInvalidError):
            approvals.verify_and_consume(
                token_id=token.id,
                signature=token.signature,
                tool_name="delete_files",
                params=THREE_FILES,
            )

    def test_changed_parameters_invalidate_the_token(self, approvals: ApprovalAuthority) -> None:
        # The attack this exists for: approve "delete these 3 files", then run
        # it against 300.
        token = approvals.issue(tool_name="delete_files", params=THREE_FILES)

        with pytest.raises(ApprovalInvalidError):
            approvals.verify_and_consume(
                token_id=token.id,
                signature=token.signature,
                tool_name="delete_files",
                params={"paths": [f"{i}.txt" for i in range(300)]},
            )

    def test_a_token_cannot_be_used_for_another_tool(self, approvals: ApprovalAuthority) -> None:
        token = approvals.issue(tool_name="web_search", params={"query": "x"})

        with pytest.raises(ApprovalInvalidError):
            approvals.verify_and_consume(
                token_id=token.id,
                signature=token.signature,
                tool_name="delete_files",
                params={"query": "x"},
            )

    def test_an_expired_token_is_refused(self, migrated: Database) -> None:
        authority = ApprovalAuthority(migrated, ttl_seconds=60)
        token = authority.issue(tool_name="delete_files", params=THREE_FILES, now=1000)

        with pytest.raises(ApprovalInvalidError):
            authority.verify_and_consume(
                token_id=token.id,
                signature=token.signature,
                tool_name="delete_files",
                params=THREE_FILES,
                now=1061,
            )

    def test_a_forged_signature_is_refused(self, approvals: ApprovalAuthority) -> None:
        token = approvals.issue(tool_name="delete_files", params=THREE_FILES)

        with pytest.raises(ApprovalInvalidError):
            approvals.verify_and_consume(
                token_id=token.id,
                signature="0" * 64,
                tool_name="delete_files",
                params=THREE_FILES,
            )

    def test_a_denial_cannot_be_used_as_an_approval(self, approvals: ApprovalAuthority) -> None:
        token = approvals.issue(tool_name="delete_files", params=THREE_FILES, approved=False)

        with pytest.raises(ApprovalInvalidError):
            approvals.verify_and_consume(
                token_id=token.id,
                signature=token.signature,
                tool_name="delete_files",
                params=THREE_FILES,
            )

    def test_an_unknown_token_is_refused(self, approvals: ApprovalAuthority) -> None:
        with pytest.raises(ApprovalInvalidError):
            approvals.verify_and_consume(
                token_id="apv_invented",
                signature="x" * 64,
                tool_name="delete_files",
                params=THREE_FILES,
            )

    def test_the_error_does_not_say_which_check_failed(self, approvals: ApprovalAuthority) -> None:
        # A caller probing for the difference is a caller trying to forge one.
        token = approvals.issue(tool_name="delete_files", params=THREE_FILES)
        messages: set[str] = set()

        for kwargs in (
            {"signature": "0" * 64, "params": THREE_FILES},
            {"signature": token.signature, "params": {"paths": ["other.txt"]}},
        ):
            with pytest.raises(ApprovalInvalidError) as excinfo:
                approvals.verify_and_consume(
                    token_id=token.id,
                    tool_name="delete_files",
                    **kwargs,  # type: ignore[arg-type]
                )
            messages.add(str(excinfo.value))

        assert len(messages) == 1

    def test_the_parameter_hash_ignores_key_order(self) -> None:
        # Without sorted keys, the same call hashes differently depending on
        # dict ordering and every approval fails for irreproducible reasons.
        assert hash_params("t", {"a": 1, "b": 2}) == hash_params("t", {"b": 2, "a": 1})


class TestAuditChain:
    def test_entries_are_chained(self, audit: AuditLog) -> None:
        first = audit.record(actor="agent", action="tool.web_search")
        second = audit.record(actor="agent", action="tool.open_app")

        assert first.prev_hash is None
        assert second.prev_hash == first.entry_hash
        assert audit.verify_chain() is None

    def test_tampering_is_detected(self, audit: AuditLog, migrated: Database) -> None:
        audit.record(actor="agent", action="tool.web_search", subject="cats")
        audit.record(actor="agent", action="tool.delete_files", subject="~/Documents")
        audit.record(actor="agent", action="tool.open_app", subject="Safari")

        # Someone edits the middle entry to hide what happened.
        with migrated.write() as conn:
            conn.execute(
                "UPDATE audit_log SET subject = 'harmless' WHERE action = 'tool.delete_files'"
            )

        assert audit.verify_chain() is not None

    def test_a_deleted_entry_is_detected(self, audit: AuditLog, migrated: Database) -> None:
        audit.record(actor="agent", action="one")
        audit.record(actor="agent", action="two")
        audit.record(actor="agent", action="three")

        with migrated.write() as conn:
            conn.execute("DELETE FROM audit_log WHERE action = 'two'")

        assert audit.verify_chain() is not None

    def test_an_empty_log_is_intact(self, audit: AuditLog) -> None:
        assert audit.verify_chain() is None

    def test_recent_returns_newest_first(self, audit: AuditLog) -> None:
        audit.record(actor="agent", action="older")
        audit.record(actor="agent", action="newer")

        assert [e.action for e in audit.recent()] == ["newer", "older"]


class TestPolicyEngine:
    def test_read_tools_run_without_asking(self, policy: PolicyEngine) -> None:
        assert policy.evaluate(SEARCH_SPEC, {"query": "barca"}).allowed is True

    def test_destructive_tools_always_ask(self, policy: PolicyEngine) -> None:
        decision = policy.evaluate(DELETE_SPEC, THREE_FILES)

        assert decision.needs_confirmation is True
        assert decision.prompt is not None

    def test_a_read_tool_is_still_logged(self, policy: PolicyEngine, audit: AuditLog) -> None:
        # "Did not ask" is not "did not tell". A web search sends the query off
        # the machine, which is squarely R5's concern.
        policy.authorise(SEARCH_SPEC, {"query": "barca transfers"})

        entries = audit.recent()
        assert entries[0].action == "tool.web_search"
        assert entries[0].subject == "barca transfers"

    def test_a_destructive_tool_without_a_token_is_not_authorised(
        self, policy: PolicyEngine
    ) -> None:
        assert policy.authorise(DELETE_SPEC, THREE_FILES).allowed is False

    def test_a_destructive_tool_with_a_valid_token_proceeds(self, policy: PolicyEngine) -> None:
        token = policy.request_approval(DELETE_SPEC, THREE_FILES)

        decision = policy.authorise(
            DELETE_SPEC,
            THREE_FILES,
            approval_id=token["id"],
            signature=token["signature"],
        )

        assert decision.allowed is True

    def test_a_token_for_different_parameters_is_rejected_at_execution(
        self, policy: PolicyEngine
    ) -> None:
        # The full path, not just the token check: approval is granted for three
        # files and execution is attempted on three hundred.
        token = policy.request_approval(DELETE_SPEC, THREE_FILES)

        with pytest.raises(ApprovalInvalidError):
            policy.authorise(
                DELETE_SPEC,
                {"paths": [f"{i}.txt" for i in range(300)]},
                approval_id=token["id"],
                signature=token["signature"],
            )

    def test_a_denial_is_recorded(self, policy: PolicyEngine, audit: AuditLog) -> None:
        # "The user said no at 14:32" is what an audit trail exists to answer.
        policy.deny(DELETE_SPEC, THREE_FILES)

        entries = audit.recent()
        assert entries[0].verdict == "deny"

    def test_the_token_carries_no_secret(self, policy: PolicyEngine) -> None:
        wire = policy.request_approval(DELETE_SPEC, THREE_FILES)
        # The signing key never leaves the process, and the params hash is not
        # something the UI needs.
        assert set(wire) == {"id", "tool_name", "nonce", "expires_at", "signature"}


class TestRegistry:
    def test_a_model_is_only_shown_tools_within_the_risk_ceiling(self) -> None:
        # A model cannot request a capability it was never shown, which is
        # cheaper than refusing the call afterwards.
        registry = ToolRegistry()
        registry.register(_stub(SEARCH_SPEC))
        registry.register(_stub(DELETE_SPEC))

        names = {t["function"]["name"] for t in registry.schema(max_risk=Risk.READ)}

        assert names == {"web_search"}
        assert len(registry.schema()) == 2

    def test_duplicate_registration_is_refused(self) -> None:
        registry = ToolRegistry()
        registry.register(_stub(SEARCH_SPEC))
        with pytest.raises(ValueError, match="already registered"):
            registry.register(_stub(SEARCH_SPEC))

    def test_the_schema_matches_the_tool_calling_shape(self) -> None:
        registry = ToolRegistry()
        registry.register(_stub(SEARCH_SPEC))
        entry = registry.schema()[0]

        assert entry["type"] == "function"
        assert entry["function"]["name"] == "web_search"
        assert "parameters" in entry["function"]


def _stub(spec: ToolSpec) -> object:
    class Stub:
        @property
        def spec(self) -> ToolSpec:
            return spec

        async def run(self, params: dict[str, object]) -> ToolResult:
            return ToolResult(ok=True, content="")

    return Stub()

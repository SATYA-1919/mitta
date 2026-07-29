"""Personality layer: register selection, guards, and the rewrite verification.

check-no-secrets: allow — the fixtures include credential-shaped strings, because
verifying that protected spans survive a rewrite requires spans worth protecting.
"""

from __future__ import annotations

import pytest

from mitta.errors import ProviderUnavailableError
from mitta.llm.models import Capabilities, ChatResult, ModelDescriptor, Usage
from mitta.personality.guards import must_not_restyle, protected_spans, verify
from mitta.personality.register import Register, classify
from mitta.personality.rewriter import PersonalityLayer


class FakeGateway:
    def __init__(self, reply: str | Exception) -> None:
        self._reply = reply
        self.calls = 0

    async def complete(self, request: object) -> ChatResult:
        self.calls += 1
        if isinstance(self._reply, Exception):
            raise self._reply
        return ChatResult(
            text=self._reply,
            model=ModelDescriptor(id="m", provider="p", capabilities=Capabilities()),
            usage=Usage(1, 1),
            latency_ms=1,
        )


def layer(reply: str | Exception, **kwargs: object) -> PersonalityLayer:
    return PersonalityLayer(FakeGateway(reply), **kwargs)  # type: ignore[arg-type]


class TestRegister:
    @pytest.mark.parametrize(
        "text",
        [
            "why does my FAISS index rebuild on every start",
            "how do i deploy this",
            "explain the difference between WAL and rollback journals",
            "getting a traceback in the migration runner",
            "def foo(): pass",
            "walk me through the architecture",
        ],
    )
    def test_technical_subjects_are_serious(self, text: str) -> None:
        # A stepped explanation delivered as "yeah just do the thing ra" is
        # useless; a casual remark delivered plainly is merely dull. The costlier
        # error decides the default.
        assert classify(text).register is Register.SERIOUS

    @pytest.mark.parametrize(
        "text",
        [
            "hey",
            "thanks",
            "nice one",
            "did you see the barca match",
            "ok cool",
        ],
    )
    def test_conversational_subjects_are_playful(self, text: str) -> None:
        assert classify(text).register is Register.PLAYFUL

    def test_code_in_the_reply_forces_serious(self) -> None:
        assert (
            classify("what did you do", response_text="```python\nx = 1\n```").register
            is Register.SERIOUS
        )

    def test_an_unrecognised_long_message_defaults_to_serious(self) -> None:
        # The failure mode of a wrongly-playful answer is worse than a
        # wrongly-plain one.
        long_text = "i have been thinking about " + "something " * 20
        assert classify(long_text).register is Register.SERIOUS

    def test_the_reason_is_reported(self) -> None:
        # Surfaced in the UI so a long reply is explicable, and a wrong call can
        # be argued with.
        assert classify("hey").reason != ""


class TestGuards:
    def test_code_paths_urls_numbers_and_ids_are_protected(self) -> None:
        text = (
            "Deleted 47 files from ~/Downloads/old, freeing 2.1 GB. "
            "See https://example.com/docs and ticket MITTA-1481. "
            "Run `make check` first.\n```sh\nrm -rf /tmp/x\n```"
        )
        spans = protected_spans(text).spans

        for expected in [
            "~/Downloads/old",
            "https://example.com/docs",
            "MITTA-1481",
            "`make check`",
        ]:
            assert any(expected in span for span in spans), f"{expected} unprotected"

    def test_a_dropped_path_is_a_violation(self) -> None:
        violations = verify("Cleaned ~/Downloads/old", "cleaned your downloads")
        assert violations != []

    def test_an_invented_number_is_a_violation(self) -> None:
        # "47" surviving is not enough — a rewrite that also adds "about 50" has
        # fabricated a fact.
        violations = verify("Deleted 47 files.", "deleted 47 files, about 50 in total")
        assert any("not present in the original" in v.reason for v in violations)

    def test_a_faithful_rewrite_passes(self) -> None:
        original = "I have deleted 47 files from ~/Downloads and freed 2.1 GB."
        assert verify(original, "deleted 47 files from ~/Downloads, freed 2.1 GB") == []

    def test_a_wildly_longer_rewrite_is_rejected(self) -> None:
        # That is not a restyle, it is a different reply.
        assert verify("Done.", "Done. " + "Additionally, " * 40) != []

    def test_gutting_a_long_reply_is_rejected(self) -> None:
        original = "The migration runs in three steps. " * 10
        assert verify(original, "done") != []

    @pytest.mark.parametrize(
        "text",
        [
            "Are you sure you want to delete these?",
            "This cannot be undone.",
            "I can't help with that.",
            "This action requires approval.",
        ],
    )
    def test_confirmations_and_refusals_are_off_limits(self, text: str) -> None:
        # ARCHITECTURE.md §7: ambiguity here is dangerous. "are you sure you
        # want to delete 47 files" must not become "shall i nuke these ra".
        assert must_not_restyle(text) is True


class TestRewrite:
    async def test_a_faithful_rewrite_is_used(self) -> None:
        result = await layer("done ra, 47 files gone").apply(
            "I have removed 47 files for you.", user_text="clean my downloads"
        )
        assert result.text == "done ra, 47 files gone"
        assert result.changed is True

    async def test_a_rewrite_that_breaks_a_guard_is_discarded(self) -> None:
        # The worst case must be a plain reply, never a wrong one.
        original = "Removed 47 files from ~/Downloads/old."
        result = await layer("cleaned up your stuff").apply(original, user_text="clean downloads")

        assert result.text == original
        assert result.changed is False
        assert result.rejected is True

    async def test_a_provider_failure_returns_the_original(self) -> None:
        original = "Here is the full explanation of the migration process."
        result = await layer(ProviderUnavailableError("down")).apply(
            original, user_text="explain migrations"
        )
        assert result.text == original
        assert result.changed is False

    async def test_disabled_is_a_true_no_op(self) -> None:
        # `intensity = 0` is documented as a no-op, not a weak rewrite.
        # Half-styling is worse than either extreme.
        gateway = FakeGateway("styled")
        result = await PersonalityLayer(gateway, intensity=0.0).apply(  # type: ignore[arg-type]
            "Some reply text here.", user_text="hey"
        )
        assert result.changed is False
        assert gateway.calls == 0

    async def test_a_confirmation_prompt_is_never_sent_for_rewriting(self) -> None:
        gateway = FakeGateway("shall i nuke these ra")
        result = await PersonalityLayer(gateway).apply(  # type: ignore[arg-type]
            "Are you sure you want to delete 47 files? This cannot be undone.",
            user_text="delete them",
        )
        assert result.changed is False
        assert gateway.calls == 0

    async def test_an_already_short_reply_is_left_alone(self) -> None:
        gateway = FakeGateway("yep")
        result = await PersonalityLayer(gateway).apply("Done.", user_text="thanks")  # type: ignore[arg-type]
        assert result.changed is False
        assert gateway.calls == 0

    async def test_an_unchanged_rewrite_reports_no_change(self) -> None:
        # `styled` records the pass ran; `changed` records it did something. The
        # UI must not swap displayed text for an identical string (DEC-046).
        original = "The index rebuilt successfully."
        result = await layer(original).apply(original, user_text="did it work")
        assert result.changed is False

    async def test_the_register_reaches_the_caller(self) -> None:
        result = await layer("sure").apply(
            "Here is a fairly long conversational reply for you.", user_text="hey how are you"
        )
        assert result.register is Register.PLAYFUL

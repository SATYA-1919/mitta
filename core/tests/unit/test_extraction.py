"""Memory extraction — what MITTA learns, and what it refuses to.

check-no-secrets: allow — the fixtures below are synthetic and must look like
real credentials, because verifying that extraction refuses those shapes is the
entire purpose of this file.
"""

from __future__ import annotations

import json

import pytest

from mitta.agent.extraction import (
    EXTRACTABLE,
    MemoryExtractor,
    looks_sensitive,
    parse_candidates,
)
from mitta.conversations.models import Message, MessageRole
from mitta.errors import ProviderUnavailableError
from mitta.llm.models import ChatResult, ModelDescriptor, Usage
from mitta.memory.models import MemoryKind
from mitta.memory.service import MemoryService
from mitta.telemetry.redaction import SecretRedactor


def message(role: MessageRole, content: str, seq: int = 1) -> Message:
    return Message(
        seq=seq,
        id=f"msg_{seq}",
        conversation_id="cnv_1",
        turn_id="trn_1",
        role=role,
        content=content,
        content_raw=None,
        tool_calls=None,
        tool_call_id=None,
        input_kind=None,
        model_id=None,
        provider=None,
        register=None,
        token_input=None,
        token_output=None,
        latency_ms=None,
        styled=False,
        error=None,
        created_at=0,
    )


class FakeGateway:
    """Returns a scripted extraction reply."""

    def __init__(self, reply: str | Exception) -> None:
        self._reply = reply
        self.calls = 0

    async def complete(self, request: object) -> ChatResult:
        self.calls += 1
        if isinstance(self._reply, Exception):
            raise self._reply
        return ChatResult(
            text=self._reply,
            model=ModelDescriptor(
                id="m",
                provider="p",
                capabilities=__import__(
                    "mitta.llm.models", fromlist=["Capabilities"]
                ).Capabilities(),
            ),
            usage=Usage(1, 1),
            latency_ms=1,
        )


def reply(*memories: dict[str, object]) -> str:
    return json.dumps({"memories": list(memories)})


EXCHANGE = [
    message(MessageRole.USER, "I always use pnpm instead of npm for my projects", 1),
    message(MessageRole.ASSISTANT, "Noted.", 2),
]


class TestSensitiveDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "Satya's Groq key is gsk_abcdefghijklmnopqrs",
            "the API key for the project is stored in .env",
            "his password is hunter2",
            "card number 4111 1111 1111 1111",
            "aadhaar 1234 5678 9012",
            "the auth token expires monthly",
            "sk-or-v1-abcdefghijklmnopqrstuv",
            "bearer eyJhbGciOiJIUzI1.eyJzdWIiOiIx.abc",
            "his passport is a British one",
        ],
    )
    def test_credentials_and_identifiers_are_refused(self, text: str) -> None:
        # A false positive costs one forgotten fact. A false negative writes a
        # credential into a database that persists for years.
        assert looks_sensitive(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Satya prefers pnpm over npm",
            "Satya supports FC Barcelona",
            "Satya is building MITTA in Python and Rust",
            "Satya's project uses SQLite and FAISS",
        ],
    )
    def test_ordinary_facts_are_allowed(self, text: str) -> None:
        assert looks_sensitive(text) is False


class TestParsing:
    def test_parses_a_well_formed_reply(self) -> None:
        candidates = parse_candidates(
            reply({"content": "Satya prefers pnpm", "kind": "preference", "confidence": 0.9})
        )
        assert len(candidates) == 1
        assert candidates[0].kind is MemoryKind.PREFERENCE

    def test_tolerates_a_fenced_reply(self) -> None:
        # Models wrap JSON in fences despite being told not to.
        fenced = (
            "```json\n"
            + reply({"content": "Satya uses uv", "kind": "long_term", "confidence": 0.8})
            + "\n```"
        )
        assert len(parse_candidates(fenced)) == 1

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "I could not find anything.",
            "[]",
            '{"memories": "not a list"}',
            '{"memories": [{"content": "x"}]}',
            '{"memories": [{"content": "", "kind": "long_term", "confidence": 1}]}',
            '{"memories": [{"content": "x", "kind": "invented", "confidence": 1}]}',
            '{"memories": [{"content": "x", "kind": "long_term", "confidence": "banana"}]}',
        ],
    )
    def test_malformed_replies_cost_this_turn_and_nothing_else(self, raw: str) -> None:
        # An exception here would fail a turn that already answered correctly.
        assert parse_candidates(raw) == []

    def test_unextractable_kinds_are_dropped(self) -> None:
        # `episodic` needs a timestamp and `relationship` a person id; free-form
        # extraction cannot supply either, and a half-populated record is worse
        # than none.
        assert MemoryKind.EPISODIC not in EXTRACTABLE
        assert (
            parse_candidates(reply({"content": "x", "kind": "episodic", "confidence": 1.0})) == []
        )

    def test_confidence_is_clamped(self) -> None:
        candidates = parse_candidates(
            reply({"content": "x", "kind": "long_term", "confidence": 5.0})
        )
        assert candidates[0].confidence == 1.0


class TestExtraction:
    async def test_learns_a_durable_preference(self, memory_service: MemoryService) -> None:
        gateway = FakeGateway(
            reply(
                {
                    "content": "Satya prefers pnpm over npm for his projects",
                    "kind": "preference",
                    "confidence": 0.95,
                }
            )
        )
        extractor = MemoryExtractor(memory_service, gateway)  # type: ignore[arg-type]

        result = await extractor.extract(EXCHANGE)

        assert result.learned == 1
        stored = memory_service.get(result.stored[0])
        assert stored.kind is MemoryKind.PREFERENCE
        # Extracted, not stated — so the user can judge a memory they do not
        # recognise.
        assert stored.source_kind.value == "conversation"

    async def test_refuses_to_store_a_credential(self, memory_service: MemoryService) -> None:
        gateway = FakeGateway(
            reply(
                {
                    "content": "Satya's Groq API key is gsk_abcdefghijklmnop",
                    "kind": "long_term",
                    "confidence": 1.0,
                }
            )
        )
        extractor = MemoryExtractor(memory_service, gateway)  # type: ignore[arg-type]

        result = await extractor.extract(EXCHANGE)

        assert result.learned == 0
        assert result.rejected_sensitive == 1
        assert memory_service.count() == 0

    async def test_refuses_a_live_secret_the_redactor_knows(
        self, memory_service: MemoryService
    ) -> None:
        # The session token has no recognisable shape, so only the redactor's
        # registered literals can catch it.
        redactor = SecretRedactor()
        redactor.register("supersecretsessionvalue123")
        gateway = FakeGateway(
            reply(
                {
                    "content": "the value supersecretsessionvalue123 was mentioned",
                    "kind": "long_term",
                    "confidence": 1.0,
                }
            )
        )
        extractor = MemoryExtractor(memory_service, gateway, redactor=redactor)  # type: ignore[arg-type]

        result = await extractor.extract(EXCHANGE)

        assert result.learned == 0
        assert result.rejected_sensitive == 1

    async def test_a_guess_is_discarded(self, memory_service: MemoryService) -> None:
        # A wrong memory is worse than a missing one: it gets recalled
        # confidently and quietly corrupts later answers.
        gateway = FakeGateway(
            reply({"content": "Satya might like Rust", "kind": "long_term", "confidence": 0.3})
        )
        extractor = MemoryExtractor(memory_service, gateway)  # type: ignore[arg-type]

        result = await extractor.extract(EXCHANGE)

        assert result.learned == 0
        assert result.rejected_low_confidence == 1

    async def test_repeating_a_fact_does_not_duplicate_it(
        self, memory_service: MemoryService
    ) -> None:
        payload = reply({"content": "Satya prefers pnpm", "kind": "preference", "confidence": 0.9})
        extractor = MemoryExtractor(memory_service, FakeGateway(payload))  # type: ignore[arg-type]
        await extractor.extract(EXCHANGE)

        second = MemoryExtractor(memory_service, FakeGateway(payload))  # type: ignore[arg-type]
        result = await second.extract(EXCHANGE)

        assert result.learned == 0
        assert result.duplicates == 1
        assert memory_service.count() == 1

    async def test_a_provider_outage_does_not_fail_the_turn(
        self, memory_service: MemoryService
    ) -> None:
        # Learning is best-effort. A turn that already answered correctly must
        # not be retroactively broken by note-taking.
        gateway = FakeGateway(ProviderUnavailableError("down"))
        extractor = MemoryExtractor(memory_service, gateway)  # type: ignore[arg-type]

        result = await extractor.extract(EXCHANGE)

        assert result.learned == 0

    async def test_an_empty_exchange_costs_no_model_call(
        self, memory_service: MemoryService
    ) -> None:
        gateway = FakeGateway(reply())
        extractor = MemoryExtractor(memory_service, gateway)  # type: ignore[arg-type]

        await extractor.extract([])

        assert gateway.calls == 0

    async def test_tool_output_is_not_sent_to_the_extractor(
        self, memory_service: MemoryService
    ) -> None:
        # Tool output is machine text and is where credentials and file contents
        # actually appear.
        gateway = FakeGateway(reply())
        extractor = MemoryExtractor(memory_service, gateway)  # type: ignore[arg-type]

        await extractor.extract([message(MessageRole.TOOL, "gsk_leakedkeyvalue", 1)])

        assert gateway.calls == 0


class TestConfidenceCoercion:
    """Models reply with words as readily as numbers, whatever the prompt says.

    Rejecting those dropped a correct memory *silently* — indistinguishable from
    "nothing worth learning", which is how the bug survived a full test suite
    and only showed up in a live conversation.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0.9, 0.9),
            (1, 1.0),
            ("high", 0.9),
            ("HIGH", 0.9),
            ("  medium  ", 0.6),
            ("low", 0.3),
            ("0.85", 0.85),
            ("85", 0.85),
            ("85%", 0.85),
            (5.0, 1.0),
            (-1, 0.0),
        ],
    )
    def test_confidence_forms_that_should_work(self, raw: object, expected: float) -> None:
        candidates = parse_candidates(
            reply({"content": "x", "kind": "long_term", "confidence": raw})
        )
        assert len(candidates) == 1
        assert candidates[0].confidence == pytest.approx(expected)

    @pytest.mark.parametrize("raw", [None, "", "banana", [], {}, True])
    def test_nonsense_is_still_rejected(self, raw: object) -> None:
        # `True` in particular: it is an int in Python and would otherwise read
        # as full confidence.
        assert (
            parse_candidates(reply({"content": "x", "kind": "long_term", "confidence": raw})) == []
        )

    def test_the_exact_reply_that_was_being_dropped(self) -> None:
        # Verbatim from llama-3.3-70b during a live conversation.
        raw = (
            '{"memories": [{"content": "The user prefers uv over pip for Python projects.", '
            '"kind": "preference", "confidence": "high"}]}'
        )
        candidates = parse_candidates(raw)
        assert len(candidates) == 1
        assert candidates[0].confidence == 0.9

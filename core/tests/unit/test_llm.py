"""LLM gateway: routing, health, failover, key handling."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from mitta.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
)
from mitta.llm import keys
from mitta.llm.gateway import LLMGateway
from mitta.llm.health import HealthPolicy, HealthTracker
from mitta.llm.models import (
    Capabilities,
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResult,
    ModelDescriptor,
    Role,
    TaskClass,
    Usage,
)
from mitta.llm.provider import select_model
from mitta.llm.providers.openai_compatible import parse_sse_line


def model(
    name: str, *, provider: str = "p", quality: int = 50, cost: float = 0.0, **caps: object
) -> ModelDescriptor:
    return ModelDescriptor(
        id=name,
        provider=provider,
        capabilities=Capabilities(**caps),  # type: ignore[arg-type]
        quality=quality,
        input_cost=cost,
        output_cost=cost,
    )


class FakeProvider:
    """A provider whose behaviour the test scripts."""

    def __init__(
        self,
        name: str,
        *,
        models: Sequence[ModelDescriptor] | None = None,
        configured: bool = True,
        fail_with: Exception | None = None,
        text: str = "ok",
        chunks: Sequence[str] | None = None,
        fail_after_chunks: int | None = None,
    ) -> None:
        self._name = name
        self._models = list(models or [model("m", provider=name)])
        self._configured = configured
        self._fail_with = fail_with
        self._text = text
        self._chunks = list(chunks or ["ok"])
        self._fail_after = fail_after_chunks
        self.calls = 0
        self.closed = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def configured(self) -> bool:
        return self._configured

    def models(self) -> Sequence[ModelDescriptor]:
        return self._models

    async def complete(self, request: ChatRequest, model: ModelDescriptor) -> ChatResult:
        self.calls += 1
        if self._fail_with is not None:
            raise self._fail_with
        return ChatResult(text=self._text, model=model, usage=Usage(1, 1), latency_ms=1)

    def stream(self, request: ChatRequest, model: ModelDescriptor) -> AsyncIterator[ChatChunk]:
        return self._stream(model)

    async def _stream(self, model: ModelDescriptor) -> AsyncIterator[ChatChunk]:
        self.calls += 1
        for index, text in enumerate(self._chunks):
            if self._fail_after is not None and index == self._fail_after:
                raise self._fail_with or ProviderUnavailableError("stream died")
            yield ChatChunk(text=text)
        if self._fail_with is not None and self._fail_after is None:
            raise self._fail_with

    async def aclose(self) -> None:
        self.closed = True


def request(**kwargs: object) -> ChatRequest:
    payload: dict[str, object] = {"messages": [ChatMessage(Role.USER, "hello")]}
    payload.update(kwargs)
    return ChatRequest(**payload)  # type: ignore[arg-type]


class TestModelSelection:
    async def test_planning_takes_the_best_model_regardless_of_cost(self) -> None:
        # This is where weak models fail, and a bad plan wastes more than a
        # better model costs.
        provider = FakeProvider(
            "p",
            models=[
                model("cheap", quality=30, cost=0.0),
                model("strong", quality=90, cost=10.0),
            ],
        )
        chosen = select_model(provider, request(), TaskClass.PLANNING)
        assert chosen is not None and chosen.id == "strong"

    async def test_personality_takes_the_cheapest(self) -> None:
        # It runs on every reply, so its latency and cost are felt directly.
        provider = FakeProvider(
            "p",
            models=[
                model("cheap", quality=30, cost=0.0),
                model("strong", quality=90, cost=10.0),
            ],
        )
        chosen = select_model(provider, request(), TaskClass.PERSONALITY)
        assert chosen is not None and chosen.id == "cheap"

    async def test_models_lacking_a_required_capability_are_excluded(self) -> None:
        provider = FakeProvider("p", models=[model("no-tools", tools=False)])
        chosen = select_model(provider, request(required=Capabilities(tools=True)), TaskClass.CHAT)
        assert chosen is None

    async def test_a_model_too_small_for_the_prompt_is_excluded(self) -> None:
        provider = FakeProvider("p", models=[model("tiny", context_window=10)])
        big = ChatRequest(messages=[ChatMessage(Role.USER, "x" * 10_000)])
        assert select_model(provider, big, TaskClass.CHAT) is None


class TestFailover:
    async def test_falls_over_to_the_secondary_on_a_rate_limit(self) -> None:
        primary = FakeProvider("groq", fail_with=ProviderRateLimitedError("429"))
        secondary = FakeProvider("openrouter", text="from the secondary")
        gateway = LLMGateway([primary, secondary])

        result = await gateway.complete(request())

        assert result.text == "from the secondary"
        # Surfaced so a reply that feels different has a visible reason.
        assert result.failover_from == "groq"

    async def test_a_healthy_primary_is_not_bypassed(self) -> None:
        primary = FakeProvider("groq", text="primary")
        secondary = FakeProvider("openrouter", text="secondary")
        gateway = LLMGateway([primary, secondary])

        result = await gateway.complete(request())

        assert result.text == "primary"
        assert result.failover_from is None
        assert secondary.calls == 0

    async def test_an_unconfigured_provider_is_skipped_silently(self) -> None:
        # An absent key is not a fault, and marking it unhealthy would show the
        # user "OpenRouter is down" when they simply never added a key.
        primary = FakeProvider("groq", configured=False)
        secondary = FakeProvider("openrouter", text="secondary")
        gateway = LLMGateway([primary, secondary])

        result = await gateway.complete(request())

        assert result.text == "secondary"
        assert gateway.health.state("groq") == "healthy"

    async def test_an_auth_failure_does_not_open_the_breaker(self) -> None:
        # The provider is fine; the credential is not. Reporting "unavailable"
        # would send the user debugging the wrong thing.
        primary = FakeProvider("groq", fail_with=ProviderAuthError("bad key"))
        secondary = FakeProvider("openrouter", text="secondary")
        gateway = LLMGateway([primary, secondary])

        await gateway.complete(request())

        assert gateway.health.state("groq") == "healthy"

    async def test_all_providers_failing_reports_every_attempt(self) -> None:
        gateway = LLMGateway(
            [
                FakeProvider("groq", fail_with=ProviderRateLimitedError("429")),
                FakeProvider("openrouter", fail_with=ProviderUnavailableError("503")),
            ]
        )

        with pytest.raises(ProviderUnavailableError) as excinfo:
            await gateway.complete(request())

        attempts = excinfo.value.details["attempts"]
        assert [a["provider"] for a in attempts] == ["groq", "openrouter"]

    async def test_no_key_at_all_is_a_distinct_error(self) -> None:
        # Three different problems with three different fixes; collapsing them
        # sends the user to the wrong one.
        gateway = LLMGateway([FakeProvider("groq", configured=False)])

        with pytest.raises(ProviderAuthError, match="Settings"):
            await gateway.complete(request())

    async def test_nothing_satisfying_the_request_is_a_distinct_error(self) -> None:
        gateway = LLMGateway([FakeProvider("groq", models=[model("m", vision=False)])])

        with pytest.raises(ProviderError) as excinfo:
            await gateway.complete(request(required=Capabilities(vision=True)))

        assert excinfo.value.details["needs_vision"] is True

    async def test_closing_the_gateway_closes_every_provider(self) -> None:
        providers = [FakeProvider("groq"), FakeProvider("openrouter")]
        await LLMGateway(providers).aclose()
        assert all(p.closed for p in providers)


class TestStreamingFailover:
    async def test_fails_over_before_the_first_token(self) -> None:
        primary = FakeProvider(
            "groq", fail_with=ProviderUnavailableError("down"), fail_after_chunks=0
        )
        secondary = FakeProvider("openrouter", chunks=["from ", "secondary"])
        gateway = LLMGateway([primary, secondary])

        text = "".join([chunk.text async for chunk in gateway.stream(request())])

        assert text == "from secondary"

    async def test_does_not_fail_over_mid_reply(self) -> None:
        # Retrying would restart the answer from the beginning, and the user
        # would watch one reply be replaced by a different one. A partial reply
        # plus a visible error is the honest outcome.
        primary = FakeProvider(
            "groq",
            chunks=["partial ", "answer"],
            fail_with=ProviderUnavailableError("died"),
            fail_after_chunks=1,
        )
        secondary = FakeProvider("openrouter", chunks=["different answer"])
        gateway = LLMGateway([primary, secondary])

        received: list[str] = []
        with pytest.raises(ProviderUnavailableError):
            async for chunk in gateway.stream(request()):
                received.append(chunk.text)

        assert received == ["partial "]
        assert secondary.calls == 0


class TestHealth:
    async def test_a_provider_is_taken_out_after_repeated_failures(self) -> None:
        tracker = HealthTracker(HealthPolicy(failure_threshold=2, cooldown_seconds=60))

        tracker.record_failure("groq", "boom")
        assert tracker.is_available("groq") is True

        tracker.record_failure("groq", "boom")
        assert tracker.is_available("groq") is False
        assert tracker.state("groq") == "unavailable"

    async def test_the_cooldown_lets_one_probe_through(self) -> None:
        tracker = HealthTracker(HealthPolicy(failure_threshold=1, cooldown_seconds=30))
        tracker.record_failure("groq", "boom", now=0.0)

        assert tracker.is_available("groq", now=29.0) is False
        assert tracker.is_available("groq", now=31.0) is True

    async def test_a_success_fully_restores_the_provider(self) -> None:
        # Not a decrement: a provider that just answered is working, and making
        # it earn its way back would prolong a fault that has already cleared.
        tracker = HealthTracker(HealthPolicy(failure_threshold=1))
        tracker.record_failure("groq", "boom")
        tracker.record_success("groq")

        assert tracker.is_available("groq") is True
        assert tracker.state("groq") == "healthy"

    async def test_a_failed_probe_backs_off_further(self) -> None:
        tracker = HealthTracker(
            HealthPolicy(failure_threshold=1, cooldown_seconds=10, max_cooldown_seconds=100)
        )
        tracker.record_failure("groq", "boom", now=0.0)
        tracker.record_failure("groq", "boom", now=11.0)  # the probe also failed

        assert tracker.is_available("groq", now=15.0) is False
        assert tracker.is_available("groq", now=32.0) is True

    async def test_the_cooldown_is_capped(self) -> None:
        tracker = HealthTracker(
            HealthPolicy(failure_threshold=1, cooldown_seconds=10, max_cooldown_seconds=40)
        )
        for _ in range(10):
            tracker.record_failure("groq", "boom", now=0.0)

        assert tracker.snapshot()["groq"].cooldown <= 40

    async def test_the_snapshot_is_a_copy(self) -> None:
        tracker = HealthTracker()
        tracker.record_failure("groq", "boom")
        snapshot = tracker.snapshot()
        tracker.record_success("groq")

        assert snapshot["groq"].consecutive_failures == 1

    async def test_a_recovered_provider_is_used_again(self) -> None:
        failing = FakeProvider("groq", fail_with=ProviderUnavailableError("down"))
        gateway = LLMGateway(
            [failing, FakeProvider("openrouter")],
            health=HealthTracker(HealthPolicy(failure_threshold=1, cooldown_seconds=0.0)),
        )

        await gateway.complete(request())
        assert gateway.health.state("groq") == "unavailable"

        # Cooldown of zero: the very next request probes it.
        failing._fail_with = None
        result = await gateway.complete(request())

        assert result.provider == "groq"
        assert gateway.health.state("groq") == "healthy"


class TestSseParsing:
    def test_parses_a_content_delta(self) -> None:
        chunk = parse_sse_line('data: {"choices":[{"delta":{"content":"hi"}}]}')
        assert chunk is not None and chunk.text == "hi"

    @pytest.mark.parametrize(
        "line",
        [
            "",
            ":keep-alive",
            "data: [DONE]",
            "data:",
            "event: ping",
            'data: {"choices":[]}',
            "data: not json at all",
        ],
    )
    def test_non_payload_lines_yield_nothing(self, line: str) -> None:
        # A malformed frame mid-stream must not abort a reply that is otherwise
        # arriving correctly.
        assert parse_sse_line(line) is None

    def test_captures_the_finish_reason(self) -> None:
        chunk = parse_sse_line('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
        assert chunk is not None and chunk.finish_reason == "stop"


class TestKeys:
    def test_a_key_with_a_trailing_newline_still_works(self) -> None:
        # The single most common way to get a 401 that looks like a revoked
        # credential.
        env = {"MITTA_GROQ_API_KEY": "gsk_abc123\n"}
        assert keys.resolve("groq", env) == "gsk_abc123"

    def test_whitespace_only_reads_as_absent(self) -> None:
        assert keys.resolve("groq", {"MITTA_GROQ_API_KEY": "   "}) is None

    def test_missing_and_unknown_providers_are_none(self) -> None:
        assert keys.resolve("groq", {}) is None
        assert keys.resolve("anthropic", {"MITTA_ANTHROPIC_API_KEY": "x"}) is None

    def test_status_never_carries_a_value(self) -> None:
        statuses = keys.status({"MITTA_GROQ_API_KEY": "gsk_secret"})
        assert all(not hasattr(s, "key") for s in statuses)
        assert "gsk_secret" not in repr(statuses)

    def test_env_file_parsing(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text(
            "\n".join(
                [
                    "# a comment",
                    "",
                    "MITTA_GROQ_API_KEY=gsk_plain",
                    'MITTA_OPENROUTER_API_KEY="sk-or-quoted"',
                    "MALFORMED",
                    "EMPTY=",
                ]
            )
        )

        loaded = keys.load_env_file(path, environ={})

        assert loaded == {
            "MITTA_GROQ_API_KEY": "gsk_plain",
            "MITTA_OPENROUTER_API_KEY": "sk-or-quoted",
        }

    def test_the_existing_environment_wins_over_the_file(self, tmp_path: Path) -> None:
        # The shell's Keychain-sourced value is already in the environment; a
        # stale .env must not silently override the key just entered in Settings.
        path = tmp_path / ".env"
        path.write_text("MITTA_GROQ_API_KEY=from_file")

        loaded = keys.load_env_file(path, environ={"MITTA_GROQ_API_KEY": "from_keychain"})

        assert loaded == {}

    def test_a_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert keys.load_env_file(tmp_path / "nope.env", environ={}) == {}


class TestToolGating:
    """When a tool-selection round-trip is worth making.

    Asking the model on every turn costs a call on the many turns that need
    nothing, and reliably produces spurious calls — a model shown a hammer will
    find a nail. Both directions of the gate matter.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "search the web for barca news",
            "look up the latest ballon d'or winner",
            "open spotify",
            "save a note called ideas.md",
            "write this down for me",
            "who won the match today",
            "what's the weather",
        ],
    )
    def test_action_requests_reach_the_selector(self, text: str) -> None:
        from mitta.agent.orchestrator import wants_tools

        assert wants_tools(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "who do i support in football",
            "hey",
            "explain how FAISS indexes work",
            "what did we decide about the schema",
            "thanks",
            "why is my code failing",
        ],
    )
    def test_ordinary_questions_skip_it(self, text: str) -> None:
        # These are answerable from memory and the model's own knowledge.
        # Offering tools here is how "who do i support" becomes a web search.
        from mitta.agent.orchestrator import wants_tools

        assert wants_tools(text) is False

    def test_the_gate_is_generous_rather_than_precise(self) -> None:
        # A false positive costs one cheap call. A false negative means the tool
        # silently never fires and the user concludes the feature is broken.
        from mitta.agent.orchestrator import wants_tools

        assert wants_tools("could you find out the current price") is True

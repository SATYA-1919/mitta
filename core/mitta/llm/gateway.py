"""The LLM gateway.

The single point at which MITTA talks to a model, and the only module in the
codebase that knows a vendor's name. Everything above it asks for a task class
and capabilities (`ARCHITECTURE.md` §6).

Failover is **health-based, not round-robin** (R3). Providers are tried in
preference order, skipping any whose circuit breaker is open, and a failure
marks the provider rather than merely retrying the request. The distinction is
practical: round-robin keeps feeding a rate-limited provider half the traffic,
so half of all requests fail while the system reports itself healthy.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass

from mitta.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderUnavailableError,
)
from mitta.llm.health import HealthTracker
from mitta.llm.models import (
    ChatChunk,
    ChatRequest,
    ChatResult,
    HealthState,
    ModelDescriptor,
    TaskClass,
)
from mitta.llm.provider import Provider, select_model
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    name: str
    configured: bool
    state: HealthState
    last_error: str | None
    model_count: int


@dataclass(frozen=True, slots=True)
class Attempt:
    """One provider tried, and what came of it. Kept for the audit record."""

    provider: str
    model: str | None
    error: str | None


class LLMGateway:
    def __init__(
        self,
        providers: Sequence[Provider],
        *,
        health: HealthTracker | None = None,
    ) -> None:
        # Order is preference order. Groq first (R3): fastest inference at
        # comparable quality, and time-to-first-token is what a desktop
        # assistant is judged on.
        self._providers = list(providers)
        self._health = health or HealthTracker()

    @property
    def health(self) -> HealthTracker:
        return self._health

    def status(self) -> list[ProviderStatus]:
        """What the UI shows so the user is never guessing who answered."""
        snapshot = self._health.snapshot()
        return [
            ProviderStatus(
                name=provider.name,
                configured=provider.configured,
                state=(snapshot[provider.name].state if provider.name in snapshot else "healthy"),
                last_error=(
                    snapshot[provider.name].last_error if provider.name in snapshot else None
                ),
                model_count=len(provider.models()),
            )
            for provider in self._providers
        ]

    @property
    def configured(self) -> bool:
        """Whether any provider can serve a request.

        False means reasoning is unavailable — the UI must say so plainly rather
        than accepting a message that will fail (R8's degradation story).
        """
        return any(provider.configured for provider in self._providers)

    def _candidates(
        self, request: ChatRequest, task: TaskClass
    ) -> list[tuple[Provider, ModelDescriptor]]:
        """Providers that could serve this request, in preference order.

        Unconfigured providers are excluded silently — an absent key is not a
        fault. Unhealthy ones are excluded by the breaker.
        """
        candidates: list[tuple[Provider, ModelDescriptor]] = []
        for provider in self._providers:
            if not provider.configured:
                continue
            if not self._health.is_available(provider.name):
                continue
            model = select_model(provider, request, task)
            if model is not None:
                candidates.append((provider, model))
        return candidates

    async def complete(self, request: ChatRequest) -> ChatResult:
        """Serve a request, failing over as needed."""
        candidates = self._candidates(request, request.task)
        if not candidates:
            raise self._no_provider_error(request)

        attempts: list[Attempt] = []
        for index, (provider, model) in enumerate(candidates):
            started = time.monotonic()
            try:
                result = await provider.complete(request, model)
            except ProviderAuthError as exc:
                # A bad key is not a transient fault. Record it and move on, but
                # do not open the breaker: the provider is fine, the credential
                # is not, and reporting "unavailable" would send the user
                # debugging the wrong thing.
                log.warning(
                    "llm.provider_auth_failed",
                    extra={"provider": provider.name, "error": str(exc)},
                )
                attempts.append(Attempt(provider.name, model.id, str(exc)))
                continue
            except ProviderError as exc:
                self._health.record_failure(provider.name, str(exc))
                log.warning(
                    "llm.provider_failed",
                    extra={
                        "provider": provider.name,
                        "model_id": model.id,
                        "error": str(exc),
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                    },
                )
                attempts.append(Attempt(provider.name, model.id, str(exc)))
                continue

            self._health.record_success(provider.name)
            if index > 0:
                log.info(
                    "llm.failover_succeeded",
                    extra={"provider": provider.name, "after": attempts[-1].provider},
                )
            # `failover_from` reaches the UI so a reply that feels different has
            # a visible reason rather than looking like the model got worse.
            return ChatResult(
                text=result.text,
                model=result.model,
                usage=result.usage,
                latency_ms=result.latency_ms,
                finish_reason=result.finish_reason,
                tool_calls=result.tool_calls,
                failover_from=attempts[-1].provider if attempts else None,
            )

        raise self._exhausted_error(attempts)

    async def stream(
        self,
        request: ChatRequest,
        *,
        on_selected: Callable[[ModelDescriptor], None] | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """Stream a completion, failing over **before** the first token only.

        `on_selected` fires with the model that actually served the reply. A
        callback rather than a field on the gateway, because two turns can
        stream concurrently and shared mutable state would attribute one turn's
        answer to the other's provider — a wrong entry in an audit record that
        exists precisely to be trusted.

        Once text has been delivered, a mid-stream failure is not retried
        against another provider: doing so would restart the reply from the
        beginning, and the user would watch one answer be replaced by a
        different one. A partial reply plus a visible error is the honest
        outcome.
        """
        candidates = self._candidates(request, request.task)
        if not candidates:
            raise self._no_provider_error(request)

        attempts: list[Attempt] = []
        for provider, model in candidates:
            delivered = False
            try:
                async for chunk in provider.stream(request, model):
                    if not delivered and on_selected is not None:
                        # Fired on the first chunk, not before the attempt: a
                        # provider that fails on connect never served anything
                        # and must not be recorded as having answered.
                        on_selected(model)
                    delivered = True
                    yield chunk
            except ProviderAuthError as exc:
                attempts.append(Attempt(provider.name, model.id, str(exc)))
                if delivered:
                    raise
                continue
            except ProviderError as exc:
                self._health.record_failure(provider.name, str(exc))
                attempts.append(Attempt(provider.name, model.id, str(exc)))
                if delivered:
                    log.warning(
                        "llm.stream_failed_mid_reply",
                        extra={"provider": provider.name, "error": str(exc)},
                    )
                    raise
                continue

            self._health.record_success(provider.name)
            return

        raise self._exhausted_error(attempts)

    # -- errors --------------------------------------------------------------- #

    def _no_provider_error(self, request: ChatRequest) -> ProviderError:
        """Distinguish "no key" from "all down" from "nothing fits".

        Three different problems with three different fixes. Collapsing them
        into one message sends the user to the wrong one.
        """
        if not any(p.configured for p in self._providers):
            return ProviderAuthError(
                "No API key is configured. Add one in Settings.",
                details={"providers": [p.name for p in self._providers]},
            )

        configured = [p for p in self._providers if p.configured]
        if not any(self._health.is_available(p.name) for p in configured):
            return ProviderUnavailableError(
                "Every configured provider is currently unavailable.",
                details={"providers": {p.name: self._health.state(p.name) for p in configured}},
            )

        return ProviderError(
            "No configured model satisfies this request.",
            details={
                "required_context": request.approximate_tokens(),
                "needs_tools": request.required.tools,
                "needs_vision": request.required.vision,
            },
        )

    @staticmethod
    def _exhausted_error(attempts: Sequence[Attempt]) -> ProviderError:
        tried = ", ".join(f"{a.provider} ({a.error})" for a in attempts)
        return ProviderUnavailableError(
            f"All providers failed: {tried}",
            details={"attempts": [{"provider": a.provider, "error": a.error} for a in attempts]},
        )

    async def aclose(self) -> None:
        for provider in self._providers:
            await provider.aclose()

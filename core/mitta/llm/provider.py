"""The provider contract.

Every vendor is normalised behind this. Anything above the gateway sees
capabilities, cost and quality — never a company (`ARCHITECTURE.md` §6).

The contract deliberately makes failure *typed*. A provider that raises a bare
`Exception` on a rate limit gives the router nothing to decide with, and the
difference between "rate-limited, try the other one" and "your key is invalid,
stop and tell the user" is the difference between working failover and an
infinite retry loop against a wall.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from mitta.llm.models import ChatChunk, ChatRequest, ChatResult, ModelDescriptor, TaskClass


@runtime_checkable
class Provider(Protocol):
    """One LLM vendor."""

    @property
    def name(self) -> str:
        """Stable identifier, written to `llm_requests.provider`."""
        ...

    @property
    def configured(self) -> bool:
        """Whether a usable key is present.

        Separate from health: an unconfigured provider is not failing, it is
        absent. Conflating them would show the user "OpenRouter is down" when
        they simply have not added a key.
        """
        ...

    def models(self) -> Sequence[ModelDescriptor]:
        """Models this provider can serve, best-first within each tier."""
        ...

    async def complete(self, request: ChatRequest, model: ModelDescriptor) -> ChatResult:
        """One non-streaming completion."""
        ...

    def stream(self, request: ChatRequest, model: ModelDescriptor) -> AsyncIterator[ChatChunk]:
        """Stream a completion.

        Returns the iterator rather than being an `async def` generator so that
        an implementation may do setup eagerly — a connection error should
        surface when the caller starts streaming, not on the first `__anext__`
        several layers away.
        """
        ...

    async def aclose(self) -> None:
        """Release connections."""
        ...


def select_model(
    provider: Provider, request: ChatRequest, task: TaskClass
) -> ModelDescriptor | None:
    """Best model from one provider for this request, or `None`.

    Ordering is by task class, because "free" and "best" genuinely conflict:

    - **Planning** takes the highest quality available. This is where weak
      models fail, and a bad plan wastes far more than a better model costs.
    - **Personality** and **titling** take the cheapest and fastest. They run on
      every reply, so their latency is felt directly.
    - Everything else prefers quality per unit cost.
    """
    candidates = [
        model
        for model in provider.models()
        if model.capabilities.satisfies(request.required)
        and model.capabilities.context_window >= request.approximate_tokens()
    ]
    if not candidates:
        return None

    if task is TaskClass.PLANNING:
        return max(candidates, key=lambda m: m.quality)

    if task in (TaskClass.PERSONALITY, TaskClass.TITLING):
        # Cost first, quality as the tie-break — among equally free models the
        # better one is still preferable.
        return min(candidates, key=lambda m: (m.input_cost + m.output_cost, -m.quality))

    return max(candidates, key=lambda m: (m.quality, -(m.input_cost + m.output_cost)))

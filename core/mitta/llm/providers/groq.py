"""Groq — primary provider (R3).

Chosen as primary for latency: Groq's inference is markedly faster than the
alternatives at comparable quality, and in a desktop assistant time-to-first-
token is the difference between a tool that feels alive and one that feels like
a web form.

The catalogue below is a **starting point, not a fixed list**. Model names move,
and a hardcoded id that has been retired presents as a 404 with no explanation.
`ARCHITECTURE.md` §6 makes model discovery a runtime concern; until that lands,
these are conservative choices with the quality ordering that matters for
routing.
"""

from __future__ import annotations

import httpx

from mitta.llm.models import Capabilities, ModelDescriptor
from mitta.llm.providers.openai_compatible import OpenAICompatibleProvider

NAME = "groq"
BASE_URL = "https://api.groq.com/openai/v1"

MODELS: tuple[ModelDescriptor, ...] = (
    ModelDescriptor(
        id="llama-3.3-70b-versatile",
        provider=NAME,
        capabilities=Capabilities(
            streaming=True,
            tools=True,
            json_mode=True,
            context_window=128_000,
            max_output_tokens=32_768,
        ),
        quality=78,
    ),
    ModelDescriptor(
        id="llama-3.1-8b-instant",
        provider=NAME,
        capabilities=Capabilities(
            streaming=True,
            tools=True,
            json_mode=True,
            context_window=128_000,
            max_output_tokens=8_192,
        ),
        # The personality rewrite runs on every reply, so it routes here: fast
        # and cheap matters more than depth for a constrained restyle (DEC-008).
        quality=55,
    ),
)


class GroqProvider(OpenAICompatibleProvider):
    def __init__(self, api_key: str | None, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(
            name=NAME,
            base_url=BASE_URL,
            api_key=api_key,
            models=MODELS,
            client=client,
        )

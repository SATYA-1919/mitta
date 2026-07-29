"""OpenRouter — secondary provider (R3).

A useful second leg specifically because it is **not a single vendor**. If Groq
is rate-limited or down, OpenRouter can serve a comparable model from a
different upstream without a code change — which is what makes failover
meaningful rather than theatrical. Two legs that share an upstream fail
together.
"""

from __future__ import annotations

import httpx

from mitta.llm.models import Capabilities, ModelDescriptor
from mitta.llm.providers.openai_compatible import OpenAICompatibleProvider

NAME = "openrouter"
BASE_URL = "https://openrouter.ai/api/v1"

MODELS: tuple[ModelDescriptor, ...] = (
    ModelDescriptor(
        id="meta-llama/llama-3.3-70b-instruct",
        provider=NAME,
        capabilities=Capabilities(
            streaming=True,
            tools=True,
            json_mode=True,
            context_window=128_000,
            max_output_tokens=16_384,
        ),
        quality=76,
    ),
    ModelDescriptor(
        id="meta-llama/llama-3.1-8b-instruct",
        provider=NAME,
        capabilities=Capabilities(
            streaming=True,
            tools=True,
            json_mode=True,
            context_window=128_000,
            max_output_tokens=8_192,
        ),
        quality=54,
    ),
)


class OpenRouterProvider(OpenAICompatibleProvider):
    def __init__(self, api_key: str | None, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(
            name=NAME,
            base_url=BASE_URL,
            api_key=api_key,
            models=MODELS,
            # OpenRouter attributes traffic by these. Deliberately identifying
            # the application and nothing about the user or the machine —
            # nothing leaves this process that is not required to serve the
            # request (R5).
            extra_headers={
                "HTTP-Referer": "https://github.com/mitta",
                "X-Title": "MITTA",
            },
            client=client,
        )

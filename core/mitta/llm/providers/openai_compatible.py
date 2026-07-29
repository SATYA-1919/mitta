"""Shared implementation for OpenAI-compatible chat APIs.

Both confirmed providers (Groq, OpenRouter) speak the OpenAI
chat-completions protocol, so the transport, the SSE parsing and — most
importantly — the mapping from HTTP status to a *typed* error live here once.

That error mapping is the load-bearing part. `429` must become
`ProviderRateLimitedError` so the router fails over; `401` must become
`ProviderAuthError` so it does not, because retrying an invalid key forever
looks exactly like a network fault and wastes the user's time on the wrong
diagnosis.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from mitta.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
)
from mitta.llm.models import (
    ChatChunk,
    ChatRequest,
    ChatResult,
    ModelDescriptor,
    Usage,
)
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)


class OpenAICompatibleProvider:
    """Base for providers speaking the OpenAI chat-completions protocol."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str | None,
        models: Sequence[ModelDescriptor],
        extra_headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._models = list(models)
        self._extra_headers = extra_headers or {}
        self._client = client
        self._owns_client = client is None

    @property
    def name(self) -> str:
        return self._name

    @property
    def configured(self) -> bool:
        return self._api_key is not None and self._api_key.strip() != ""

    def models(self) -> Sequence[ModelDescriptor]:
        return self._models

    # -- transport ----------------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        if self._api_key is None:
            raise ProviderAuthError(f"No API key configured for {self._name}")
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        return self._client

    def _payload(self, request: ChatRequest, model: ModelDescriptor) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model.id,
            "messages": [message.to_wire() for message in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = min(request.max_tokens, model.capabilities.max_output_tokens)
        if request.stop:
            payload["stop"] = request.stop
        if request.tools and model.capabilities.tools:
            payload["tools"] = request.tools
        return payload

    # -- completion ----------------------------------------------------------- #

    async def complete(self, request: ChatRequest, model: ModelDescriptor) -> ChatResult:
        started = time.monotonic()
        payload = self._payload(request, model) | {"stream": False}

        try:
            response = await self._http().post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError(
                f"{self._name} timed out", details={"provider": self._name}
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"{self._name} is unreachable: {exc}", details={"provider": self._name}
            ) from exc

        self._raise_for_status(response)
        body = response.json()

        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = body.get("usage") or {}

        return ChatResult(
            text=message.get("content") or "",
            model=model,
            usage=Usage(
                tokens_in=int(usage.get("prompt_tokens") or 0),
                tokens_out=int(usage.get("completion_tokens") or 0),
            ),
            latency_ms=int((time.monotonic() - started) * 1000),
            finish_reason=choice.get("finish_reason"),
            tool_calls=message.get("tool_calls"),
        )

    def stream(self, request: ChatRequest, model: ModelDescriptor) -> AsyncIterator[ChatChunk]:
        return self._stream(request, model)

    async def _stream(
        self, request: ChatRequest, model: ModelDescriptor
    ) -> AsyncIterator[ChatChunk]:
        payload = self._payload(request, model) | {"stream": True}

        try:
            async with self._http().stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    # The body must be read before it can be inspected; a
                    # streaming response has not buffered it.
                    await response.aread()
                    self._raise_for_status(response)

                async for line in response.aiter_lines():
                    chunk = parse_sse_line(line)
                    if chunk is not None:
                        yield chunk
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError(
                f"{self._name} timed out", details={"provider": self._name}
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"{self._name} is unreachable: {exc}", details={"provider": self._name}
            ) from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Map HTTP status onto the typed errors the router decides with."""
        if response.status_code < 400:
            return

        detail = _error_message(response)
        context = {"provider": self._name, "status": response.status_code}

        if response.status_code in (401, 403):
            # Not retryable and not a failover trigger in the usual sense: a bad
            # key stays bad, and retrying it forever presents as a network fault.
            raise ProviderAuthError(f"{self._name} rejected the API key: {detail}", details=context)
        if response.status_code == 429:
            raise ProviderRateLimitedError(
                f"{self._name} is rate-limiting: {detail}", details=context
            )
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                f"{self._name} returned {response.status_code}: {detail}", details=context
            )
        raise ProviderError(f"{self._name} rejected the request: {detail}", details=context)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


def _error_message(response: httpx.Response) -> str:
    """Pull a human-readable message out of an error body.

    Falls back to the raw text, truncated. Provider error bodies are not a
    stable contract, and an exception raised while formatting an exception is a
    genuinely miserable thing to debug.
    """
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return response.text[:200]

    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message[:200]
        if isinstance(error, str):
            return error[:200]
    return str(body)[:200]


def parse_sse_line(line: str) -> ChatChunk | None:
    """Parse one server-sent-events line into a chunk.

    Returns `None` for anything that is not a payload — keep-alives, blank
    lines, comments and the terminal `[DONE]` sentinel. Separated out because
    it is pure and therefore testable without a network.
    """
    if not line or not line.startswith("data:"):
        return None

    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return None

    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        # A malformed frame mid-stream must not abort a reply that is otherwise
        # arriving correctly.
        log.debug("llm.malformed_sse_frame", extra={"raw": data[:120]})
        return None

    choices = payload.get("choices") or []
    if not choices:
        return None

    choice = choices[0]
    delta = choice.get("delta") or {}
    return ChatChunk(
        text=delta.get("content") or "",
        tool_calls=delta.get("tool_calls"),
        finish_reason=choice.get("finish_reason"),
    )

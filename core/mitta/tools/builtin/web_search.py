"""Web search.

`READ` risk: it changes nothing. It is still logged and surfaced, because the
query leaves the machine — which is exactly what R5 is about. The user gets to
see what was searched on their behalf.

Uses DuckDuckGo's HTML endpoint rather than a keyed search API: no third
account, no second credential to store, and nothing in the request identifies
the user beyond the query itself.
"""

from __future__ import annotations

import html
import re
from typing import Any, Final

import httpx

from mitta.tools.base import Risk, ToolResult, ToolSpec

ENDPOINT: Final = "https://html.duckduckgo.com/html/"
MAX_RESULTS: Final = 5

_RESULT = re.compile(
    r'class="result__a"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'.*?class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.DOTALL,
)
_TAGS = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    return html.unescape(_TAGS.sub("", fragment)).strip()


def parse_results(body: str, limit: int = MAX_RESULTS) -> list[dict[str, str]]:
    """Extract results from the HTML.

    Separated out because it is the fragile part — a scraped page changes shape
    without notice — and pure functions can be tested against a saved fixture
    rather than against the live internet.
    """
    return [
        {
            "title": _clean(match.group("title")),
            "url": match.group("url"),
            "snippet": _clean(match.group("snippet"))[:300],
        }
        for match in list(_RESULT.finditer(body))[:limit]
    ]


class WebSearchTool:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="web_search",
            description=(
                "Search the web for current information. Use this for anything "
                "you do not know or that may have changed recently."
            ),
            risk=Risk.READ,
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to search for"}},
                "required": ["query"],
            },
        )

    async def run(self, params: dict[str, Any]) -> ToolResult:
        query = str(params.get("query", "")).strip()
        if not query:
            return ToolResult.failure("No search query given.")

        client = self._client
        created = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=15.0)

        try:
            response = await client.post(
                ENDPOINT,
                data={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (Macintosh) MITTA/0.1"},
            )
            response.raise_for_status()
            body = response.text
        except httpx.HTTPError as exc:
            return ToolResult.failure(f"Search failed: {exc}")
        finally:
            if created:
                await client.aclose()

        results = parse_results(body)
        if not results:
            return ToolResult(
                ok=True,
                content=f"No results found for {query!r}.",
                detail={"query": query, "count": 0},
            )

        rendered = "\n\n".join(f"{r['title']}\n{r['url']}\n{r['snippet']}" for r in results)
        return ToolResult(
            ok=True,
            content=rendered,
            detail={"query": query, "count": len(results), "results": results},
        )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()

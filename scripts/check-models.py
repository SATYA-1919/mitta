#!/usr/bin/env python3
"""Verify the hardcoded model ids still exist on the live providers.

Model names get retired. A dead id presents as a 404 whose body says something
unhelpful, at the exact moment a user is waiting for an answer — and the
failover chain will dutifully try the next provider, so it looks like an outage
rather than a stale constant.

Not run at startup: that would put a network call in the boot path and make
launching depend on connectivity. This is a diagnostic, run when keys exist.

    make check-models
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

import httpx  # noqa: E402

from mitta.llm import keys  # noqa: E402
from mitta.llm.providers.groq import MODELS as GROQ_MODELS  # noqa: E402
from mitta.llm.providers.openrouter import MODELS as OPENROUTER_MODELS  # noqa: E402


async def live_ids(client: httpx.AsyncClient, url: str, token: str | None) -> set[str] | None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"  could not reach {url}: {exc}")
        return None
    return {entry["id"] for entry in response.json().get("data", [])}


async def main() -> int:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.is_file():
        keys.apply_env_file(env_file)

    failures = 0
    async with httpx.AsyncClient(timeout=20) as client:
        checks = [
            ("groq", "https://api.groq.com/openai/v1/models", keys.resolve("groq"), GROQ_MODELS),
            ("openrouter", "https://openrouter.ai/api/v1/models", None, OPENROUTER_MODELS),
        ]
        for name, url, token, declared in checks:
            if name == "groq" and token is None:
                print(f"{name}: no key — skipped")
                continue

            live = await live_ids(client, url, token)
            if live is None:
                continue

            print(f"{name}: {len(live)} models available")
            for model in declared:
                if model.id in live:
                    print(f"  ok    {model.id}")
                else:
                    print(f"  GONE  {model.id}  ← update the catalogue in providers/{name}.py")
                    failures += 1

    if failures:
        print(f"\n✗ {failures} declared model id(s) no longer exist")
        return 1
    print("\n✓ every declared model id is live")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

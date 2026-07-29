#!/usr/bin/env python
"""Download the local embedding model.

The one network call the memory engine can make, and it happens only when this
is run. `bootstrap.py` never downloads: it uses the model if present and falls
back to the deterministic provider if not, so a first run is never silently
blocked on a 67 MB fetch from a third-party host (R5, DEC-050).

    python scripts/download-model.py [--storage-root PATH]

Idempotent. Re-running with the model already present verifies it loads and
exits without touching the network.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from mitta.config.paths import Paths  # noqa: E402
from mitta.config.settings import load_settings  # noqa: E402
from mitta.memory.embedding.local import DEFAULT_MODEL, LocalEmbedder  # noqa: E402
from mitta.os_adapter.factory import create_os_adapter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-root", type=Path, default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    if args.storage_root is not None:
        paths = Paths(
            storage_root=args.storage_root,
            runtime_dir=args.storage_root / "runtime",
            log_dir=args.storage_root / "logs",
        )
    else:
        adapter = create_os_adapter()
        from mitta.config.paths import resolve_paths

        paths = resolve_paths(load_settings(), adapter)
    paths.ensure()

    embedder = LocalEmbedder(paths.models, model_id=args.model)
    already = embedder.is_available()
    print(f"model      {args.model}")
    print(f"cache      {paths.models}")
    print(f"present    {already}")

    if not already:
        print("\nDownloading (~67 MB, MIT licensed, from Hugging Face)…")
    started = time.monotonic()
    embedder.download()
    print(f"ready in   {time.monotonic() - started:.1f}s")

    # Prove it actually works rather than merely that files landed on disk.
    query = embedder.embed_query("kubernetes deployment")
    related, unrelated = embedder.embed_documents(
        ["the kubernetes deployment failed", "my cat is called mochi"]
    )
    close = float(query @ related)
    far = float(query @ unrelated)

    print(f"\ndim        {query.shape[0]}")
    print(f"related    {close:.3f}")
    print(f"unrelated  {far:.3f}")

    if close <= far:
        print("\nFAILED: the model does not separate related from unrelated text.")
        return 1

    print("\nOK. Restart the sidecar; every vector will be re-embedded automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

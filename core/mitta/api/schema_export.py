"""Export the OpenAPI document (DEC-028).

Pydantic models in `api/schemas/` are the single source of truth for the
frontend's types. This module dumps the document FastAPI derives from them;
`scripts/gen-types.sh` feeds it to `openapi-typescript`.

Generating from the runtime-validating side means the TypeScript cannot describe
a shape the server would reject. Hand-maintaining both sides guarantees drift,
and drift surfaces as a runtime error in production rather than a build error
in CI.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from mitta.api.app import create_app
from mitta.config.paths import Paths
from mitta.config.settings import Settings
from mitta.conversations.repository import ConversationRepository
from mitta.llm.gateway import LLMGateway
from mitta.llm.providers.groq import GroqProvider
from mitta.llm.providers.openrouter import OpenRouterProvider
from mitta.memory.embedding.deterministic import DeterministicEmbedder
from mitta.memory.indexer import Indexer
from mitta.memory.repository import MemoryRepository
from mitta.memory.service import MemoryService
from mitta.memory.vectors.store import VectorStore, build_index
from mitta.os_adapter.mac import MacAdapter
from mitta.persistence.database import Database
from mitta.policy.audit import AuditLog
from mitta.projects.boundary import PathBoundary
from mitta.projects.repository import ProjectRepository


def build_openapi() -> dict[str, Any]:
    """Construct the app purely to read its schema.

    Nothing is connected: `create_app` only stores its collaborators, and schema
    generation reads route signatures. Requiring a live database to emit a type
    definition would make the codegen step fail in CI for no reason.

    **Every optional router must be mounted here.** `create_app` omits routers
    whose collaborators are absent, which is right at runtime and silently wrong
    for codegen: an unmounted router produces no paths, the generated types lose
    those endpoints, and nothing fails — the frontend simply cannot see half the
    API. `assert_complete` below is what turns that into a build error.
    """
    root = Path(tempfile.gettempdir()) / "mitta-schema-export"
    paths = Paths(storage_root=root, runtime_dir=root, log_dir=root)
    settings = Settings(storage_root=root, dev_mode=True)
    database = Database(paths.database, settings.database)

    embedder = DeterministicEmbedder()
    repository = MemoryRepository(database)
    store = VectorStore(database, build_index(paths.vectors / "schema.faiss", embedder), embedder)
    projects = ProjectRepository(database)
    app = create_app(
        settings=settings,
        paths=paths,
        database=database,
        os_adapter=MacAdapter(),
        memory=MemoryService(repository, store, Indexer(repository, store)),
        # No indexer passed to the app: nothing here is connected, and a
        # background thread writing to an unopened database would be a strange
        # way to generate a type definition.
        indexer=None,
        embedder=embedder,
        gateway=LLMGateway([GroqProvider(None), OpenRouterProvider(None)]),
        conversations=ConversationRepository(database),
        projects=projects,
        path_boundary=PathBoundary(projects),
        audit=AuditLog(database),
    )
    document: dict[str, Any] = app.openapi()
    _assert_complete(document)
    return document


# Prefixes the frontend depends on. Listed explicitly so that adding a router
# and forgetting to mount it here fails the build instead of quietly shrinking
# the generated types.
REQUIRED_PATH_PREFIXES = (
    "/v1/status",
    "/v1/capabilities",
    "/v1/memory",
    "/v1/providers",
    "/v1/conversations",
    "/v1/projects",
)


def _assert_complete(document: dict[str, Any]) -> None:
    paths = document.get("paths", {})
    missing = [
        prefix
        for prefix in REQUIRED_PATH_PREFIXES
        if not any(path.startswith(prefix) for path in paths)
    ]
    if missing:
        raise RuntimeError(
            f"OpenAPI export is missing {missing}. A router was added without being "
            f"mounted in schema_export.build_openapi, so the generated TypeScript "
            f"would silently omit those endpoints."
        )


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    document = json.dumps(build_openapi(), indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(document)
    else:
        output.write_text(document, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

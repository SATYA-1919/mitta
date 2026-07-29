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
from mitta.os_adapter.mac import MacAdapter
from mitta.persistence.database import Database


def build_openapi() -> dict[str, Any]:
    """Construct the app purely to read its schema.

    Nothing is connected: `create_app` only stores its collaborators, and schema
    generation reads route signatures. Requiring a live database to emit a type
    definition would make the codegen step fail in CI for no reason.
    """
    root = Path(tempfile.gettempdir()) / "mitta-schema-export"
    paths = Paths(storage_root=root, runtime_dir=root, log_dir=root)
    settings = Settings(storage_root=root, dev_mode=True)
    app = create_app(
        settings=settings,
        paths=paths,
        database=Database(paths.database, settings.database),
        os_adapter=MacAdapter(),
    )
    return app.openapi()


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

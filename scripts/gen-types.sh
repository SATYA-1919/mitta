#!/usr/bin/env bash
#
# Regenerate the frontend's API types from the Python schemas (DEC-028).
#
# Pydantic is authoritative. Generated files are committed so the frontend
# builds without a Python environment, and CI fails if regenerating produces a
# diff — that check is what stops the committed copy from going stale.
#
# Nobody edits src/types/generated/ by hand.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
SCHEMA="$ROOT/core/openapi.json"
OUTPUT="$ROOT/apps/desktop/src/types/generated/api.ts"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: python not found at $PYTHON — run 'make install' first" >&2
  exit 1
fi

echo "→ exporting OpenAPI document"
(cd "$ROOT/core" && "$PYTHON" -m mitta.api.schema_export "$SCHEMA")

echo "→ generating TypeScript"
mkdir -p "$(dirname "$OUTPUT")"
(cd "$ROOT/apps/desktop" && npx --no-install openapi-typescript "$SCHEMA" -o "$OUTPUT")

# The generator emits a bare module; the provenance note is what stops someone
# opening the file and "fixing" it.
TMP="$(mktemp)"
{
  echo "/**"
  echo " * GENERATED FILE — DO NOT EDIT."
  echo " *"
  echo " * Produced by scripts/gen-types.sh from the Pydantic schemas in"
  echo " * core/mitta/api/schemas/ (DEC-028). To change a type, change the"
  echo " * Pydantic model and regenerate."
  echo " */"
  echo
  cat "$OUTPUT"
} >"$TMP"
mv "$TMP" "$OUTPUT"

echo "✓ $OUTPUT"

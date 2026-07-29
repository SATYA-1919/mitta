#!/usr/bin/env bash
#
# Write a provider API key into .env, safely.
#
# Exists because editing .env by hand keeps failing in ways that are silent: an
# unsaved editor buffer, a paste into .env.example, a stray quote, a trailing
# space. None of those announce themselves — the key is simply absent, and the
# app reports "not configured" with no clue why.
#
# Input is read with `read -s`, so the key is never echoed to the terminal and
# never enters shell history.
#
#   ./scripts/set-key.sh groq
#   ./scripts/set-key.sh openrouter
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"

PROVIDER="${1:-}"
case "$PROVIDER" in
  groq)       VAR="MITTA_GROQ_API_KEY";       HINT="https://console.groq.com/keys" ;;
  openrouter) VAR="MITTA_OPENROUTER_API_KEY"; HINT="https://openrouter.ai/keys" ;;
  *)
    echo "usage: $0 {groq|openrouter}" >&2
    exit 2
    ;;
esac

if [[ ! -f "$ENV_FILE" ]]; then
  cp "${ROOT}/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "created .env from the template"
fi

echo "Paste your ${PROVIDER} key (${HINT})."
echo "Nothing will appear as you type or paste. Press Enter when done."
printf '  %s = ' "$VAR"
read -rs KEY
echo

# Trim surrounding whitespace and quotes. A key pasted with a trailing newline
# or wrapped in quotes is the single most common cause of a 401 that looks like
# a revoked credential.
KEY="$(printf '%s' "$KEY" | tr -d '\r\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"

if [[ -z "$KEY" ]]; then
  echo "✗ nothing entered; .env unchanged" >&2
  exit 1
fi

# Shape check. Not validation — only the provider can say whether a key works —
# but a paste that grabbed the wrong clipboard entry is worth catching here
# rather than as an authentication failure three steps later.
case "$PROVIDER" in
  groq)
    [[ "$KEY" == gsk_* ]] || echo "  note: Groq keys usually start with 'gsk_'. Continuing anyway."
    ;;
  openrouter)
    [[ "$KEY" == sk-or-* ]] || echo "  note: OpenRouter keys usually start with 'sk-or-'. Continuing anyway."
    ;;
esac

# Rewrite in place via a 0600 temp file in the same directory, then rename.
# Writing directly would leave the file truncated if this is interrupted, and a
# temp file in /tmp could be world-readable for the moment it exists.
TMP="$(mktemp "${ROOT}/.env.XXXXXX")"
chmod 600 "$TMP"
trap 'rm -f "$TMP"' EXIT

if grep -q "^${VAR}=" "$ENV_FILE"; then
  # `awk` rather than `sed -i`: the key may contain characters sed would treat
  # as delimiters or backreferences, and mangling a credential silently is
  # worse than not writing it.
  awk -v var="$VAR" -v val="$KEY" \
    'index($0, var "=") == 1 { print var "=" val; next } { print }' \
    "$ENV_FILE" > "$TMP"
else
  cat "$ENV_FILE" > "$TMP"
  printf '%s=%s\n' "$VAR" "$KEY" >> "$TMP"
fi

mv "$TMP" "$ENV_FILE"
chmod 600 "$ENV_FILE"
trap - EXIT
unset KEY

# Verify by reading it back — length and prefix only, never the value.
python3 - "$ENV_FILE" "$VAR" <<'PYEOF'
import sys, pathlib
env_file, var = sys.argv[1], sys.argv[2]
for line in pathlib.Path(env_file).read_text().splitlines():
    if line.startswith(var + "="):
        value = line.split("=", 1)[1]
        if value:
            print(f"✓ {var} written — {len(value)} chars, starts {value[:4]}…")
            sys.exit(0)
print(f"✗ {var} is still empty", file=sys.stderr)
sys.exit(1)
PYEOF

echo
echo "Next: make dev   (or 'make shell-run' for the desktop app)"

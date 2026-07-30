#!/usr/bin/env bash
#
# Run MITTA as a desktop app.
#
# A debug build loads the frontend from `devUrl` (the Vite dev server), which is
# baked into the Tauri context at compile time. Running `cargo run` on its own
# therefore opens a window pointing at a server that is not there — a blank
# white page with nothing in the log to explain it. This starts Vite first.
#
# Unlike `scripts/dev.sh`, no session token is written to a file: the Rust shell
# spawns the sidecar itself and hands the token to the webview over IPC, which
# is the real path (DEC-060). Vite is only serving the frontend assets here.
#
#   ./scripts/app.sh              # debug, fast to build, hot-reloads the UI
#   ./scripts/app.sh --release    # optimised standalone build, slow first time
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UI="${ROOT}/apps/desktop"
SHELL_DIR="${UI}/src-tauri"
CARGO="${CARGO:-$HOME/.cargo/bin/cargo}"

RELEASE=0
for arg in "$@"; do
  case "$arg" in
    --release) RELEASE=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ ! -x "$CARGO" ]]; then
  echo "cargo not found at ${CARGO}." >&2
  echo "Install Rust:  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh" >&2
  exit 1
fi


# Give the binary a stable identity, so macOS permissions survive a rebuild.
#
# The linker's own ad-hoc signature carries an identifier derived from the
# build — `mitta-a0dab33572f5e733` one compile, `mitta-7473b53e5afff6c6` the
# next. TCC records a microphone grant against that identifier, so every
# rebuild produced a binary macOS had never seen: System Settings kept showing
# MITTA switched on for both permissions while the app reported neither.
#
# Re-signing ad-hoc with a fixed identifier makes the grant stick to the
# application rather than to one compile of it. Still ad-hoc — a real signature
# needs a Developer ID, and this is a build you run from source.
sign_for_tcc() {
  local binary="$1"
  codesign --force --sign - --identifier com.mitta.desktop "$binary" 2>/dev/null || {
    echo "  ! could not sign ${binary}; microphone access may be re-prompted" >&2
  }
}

cleanup() {
  local code=$?
  [[ -n "${VITE_PID:-}" ]] && kill "$VITE_PID" 2>/dev/null || true
  exit $code
}
trap cleanup EXIT INT TERM

if [[ "$RELEASE" == "1" ]]; then
  # A release build embeds `frontendDist`, so it needs no server and runs
  # standalone afterwards. The first compile is slow — lto and one codegen unit.
  echo "▸ building the frontend…"
  (cd "$UI" && npm run build >/dev/null)
  echo "▸ building the app (this takes a few minutes the first time)…"
  cd "$SHELL_DIR"
  "$CARGO" build --release
  sign_for_tcc "${SHELL_DIR}/target/release/mitta"
  exec "${SHELL_DIR}/target/release/mitta"
fi

echo "▸ starting the dev server…"
(cd "$UI" && npm run dev >/dev/null 2>&1) &
VITE_PID=$!

for _ in $(seq 1 100); do
  if curl -sf -o /dev/null http://127.0.0.1:1420/; then
    break
  fi
  if ! kill -0 "$VITE_PID" 2>/dev/null; then
    echo "✗ the dev server exited during startup" >&2
    exit 1
  fi
  sleep 0.2
done

if ! curl -sf -o /dev/null http://127.0.0.1:1420/; then
  echo "✗ the dev server never came up on 1420" >&2
  exit 1
fi

echo "  frontend ready on http://127.0.0.1:1420"
echo "▸ launching MITTA…"
echo
cd "$SHELL_DIR"
"$CARGO" build
sign_for_tcc "${SHELL_DIR}/target/debug/mitta"
"${SHELL_DIR}/target/debug/mitta"

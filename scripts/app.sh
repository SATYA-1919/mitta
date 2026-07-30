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

# Wrap the binary in a real .app, because TCC will not read anything else.
#
# Signing with a stable identifier (above) fixes *which* app the grant attaches
# to. It does not give TCC the usage descriptions, and without those the process
# is killed outright the first time it touches speech — `Abort trap: 6`, with
# `termination namespace TCC` in the crash report and nothing in our own logs.
#
# `build.rs` embeds Info.plist into the executable's `__TEXT,__info_plist`
# section, which is valid, present and signed — and TCC ignores it. The usage
# descriptions are only honoured from an `Info.plist` *file* inside a bundle, so
# a bare `cargo run` can never be granted microphone access no matter how it is
# signed. Hence a bundle, assembled around the binary cargo just built.
#
# Cheap enough to redo every launch: a directory, a copy and a plist. The
# alternative is `tauri build`, which is a release compile and minutes per run.
bundle_for_tcc() {
  local binary="$1"
  local app="${SHELL_DIR}/target/MITTA.app"

  rm -rf "$app"
  mkdir -p "${app}/Contents/MacOS"
  cp "$binary" "${app}/Contents/MacOS/mitta"
  cp "${SHELL_DIR}/Info.plist" "${app}/Contents/Info.plist"

  # `CFBundleExecutable` is what launchd runs, and it is absent from the source
  # plist because the embedded-section copy has no bundle to name it for.
  /usr/libexec/PlistBuddy -c "Add :CFBundleExecutable string mitta" \
    "${app}/Contents/Info.plist" >/dev/null 2>&1 || true
  /usr/libexec/PlistBuddy -c "Add :CFBundlePackageType string APPL" \
    "${app}/Contents/Info.plist" >/dev/null 2>&1 || true

  # Sign the bundle, not the inner binary: TCC identifies the .app.
  codesign --force --deep --sign - --identifier com.mitta.desktop "$app" 2>/dev/null || {
    echo "  ! could not sign the bundle; microphone access may be re-prompted" >&2
  }
  echo "$app"
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
  APP="$(bundle_for_tcc "${SHELL_DIR}/target/release/mitta")"
  exec "${APP}/Contents/MacOS/mitta"
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
APP="$(bundle_for_tcc "${SHELL_DIR}/target/debug/mitta")"
echo "  bundled at ${APP}"

# Launched through LaunchServices, not by running the inner binary.
#
# Executing `Contents/MacOS/mitta` directly leaves the process unassociated with
# its bundle, so TCC still sees a bare executable, still finds no usage
# description, and still kills it — the bundle was necessary but not sufficient.
# `open` is what makes macOS treat this as the application com.mitta.desktop.
#
# The cost is that the shell's own stdout no longer arrives here; it goes to the
# unified log. `log stream --predicate 'process == "mitta"'` reads it, and the
# sidecar's log is unaffected because Python writes its own file.
echo "  launching via LaunchServices — shell logs go to Console.app"
open -W "$APP"

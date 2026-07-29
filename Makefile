# MITTA — single entry point for every development task.
#
# Covers the Python sidecar and the frontend. Tauri targets land in Phase 4b,
# once the Rust toolchain is installed.

PY      ?= .venv/bin/python
CORE    := core
VENV    := .venv
UI      := apps/desktop
SHELL_DIR := apps/desktop/src-tauri
CARGO   ?= $(HOME)/.cargo/bin/cargo

.DEFAULT_GOAL := help
.PHONY: help venv install install-ui test test-unit test-integration lint typecheck arch \
        check check-ui check-all run clean \
        dev dev-real ui-dev ui-build ui-test ui-typecheck ui-budget gen-types download-model \
        shell-build shell-test shell-lint shell-run check-shell

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

$(VENV):
	python3 -m venv $(VENV)

venv: $(VENV)  ## Create the virtual environment

install: venv  ## Install the sidecar with dev dependencies
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -e "$(CORE)[dev]"

test:  ## Run the full test suite
	cd $(CORE) && ../$(PY) -m pytest

test-unit:  ## Unit tests only — fast
	cd $(CORE) && ../$(PY) -m pytest tests/unit

test-integration:  ## Spawn the real sidecar process and drive it
	cd $(CORE) && ../$(PY) -m pytest tests/integration -v

lint:  ## Ruff
	cd $(CORE) && ../$(PY) -m ruff check .

format:  ## Ruff, with fixes applied
	cd $(CORE) && ../$(PY) -m ruff check . --fix && ../$(PY) -m ruff format .

typecheck:  ## mypy --strict
	cd $(CORE) && ../$(PY) -m mypy mitta

arch:  ## Verify the layer dependency contracts (DEC-029)
	cd $(CORE) && ../$(VENV)/bin/lint-imports --config importlinter.ini

check: lint typecheck arch test  ## Backend: everything CI runs

# ── Frontend ───────────────────────────────────────────────────────────────

install-ui:  ## Install frontend dependencies
	cd $(UI) && npm install

dev:  ## Run sidecar + UI together on a scratch storage root
	./scripts/dev.sh

dev-real:  ## Same, against your real MITTA data
	./scripts/dev.sh --real-storage

ui-dev:  ## Vite dev server alone (needs a sidecar already running)
	cd $(UI) && npm run dev

ui-typecheck:  ## tsc --noEmit, strict
	cd $(UI) && npx tsc --noEmit

ui-test:  ## Vitest
	cd $(UI) && npx vitest run

ui-build:  ## Production bundles
	cd $(UI) && npx vite build

ui-budget: ui-build  ## Enforce the command palette's bundle budget (R2)
	node scripts/check-palette-budget.mjs

download-model:  ## Fetch the local embedding model (~67 MB, explicit — DEC-050)
	$(PY) scripts/download-model.py

gen-types:  ## Regenerate frontend types from the Pydantic schemas (DEC-028)
	./scripts/gen-types.sh

check-ui: ui-typecheck ui-test ui-budget  ## Frontend: everything CI runs

# ── Shell (Rust/Tauri) ─────────────────────────────────────────────────────

shell-build:  ## Build the Tauri shell
	cd $(SHELL_DIR) && $(CARGO) build

shell-test:  ## Rust unit tests
	cd $(SHELL_DIR) && $(CARGO) test

shell-lint:  ## rustfmt --check and clippy, warnings denied
	cd $(SHELL_DIR) && $(CARGO) fmt --check
	cd $(SHELL_DIR) && $(CARGO) clippy --all-targets -- -D warnings

shell-run: ui-build  ## Run the desktop app (builds the frontend first)
	cd $(SHELL_DIR) && $(CARGO) run

check-shell: shell-lint shell-test  ## Shell: everything CI runs

check-all: check check-ui check-shell  ## All three runtimes

run:  ## Run the sidecar in dev mode against a scratch storage root
	cd $(CORE) && MITTA_STORAGE_ROOT=$${MITTA_STORAGE_ROOT:-/tmp/mitta-dev} \
		../$(PY) -m mitta --dev

clean:  ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf $(CORE)/.pytest_cache $(CORE)/.mypy_cache $(CORE)/.ruff_cache
	rm -rf $(CORE)/*.egg-info $(CORE)/build $(CORE)/dist

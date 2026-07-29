# MITTA — single entry point for every development task.
#
# Phase 3 covers the Python sidecar only. Frontend and Tauri targets are added
# in Phase 4, once the Rust toolchain is installed.

PY      ?= .venv/bin/python
CORE    := core
VENV    := .venv

.DEFAULT_GOAL := help
.PHONY: help venv install test test-unit test-integration lint typecheck arch check run clean

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

check: lint typecheck arch test  ## Everything CI runs

run:  ## Run the sidecar in dev mode against a scratch storage root
	cd $(CORE) && MITTA_STORAGE_ROOT=$${MITTA_STORAGE_ROOT:-/tmp/mitta-dev} \
		../$(PY) -m mitta --dev

clean:  ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf $(CORE)/.pytest_cache $(CORE)/.mypy_cache $(CORE)/.ruff_cache
	rm -rf $(CORE)/*.egg-info $(CORE)/build $(CORE)/dist

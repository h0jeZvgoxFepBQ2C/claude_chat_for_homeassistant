.PHONY: setup test test-watch lint format ha-up ha-down ha-restart ha-logs ha-shell ha-clean help

PY ?= python3
VENV := .venv
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff

help:
	@echo "Common targets:"
	@echo "  setup         create venv and install test deps"
	@echo "  test          run pytest"
	@echo "  test-watch    re-run pytest on file changes (requires pytest-watch)"
	@echo "  lint          run ruff check"
	@echo "  format        run ruff format"
	@echo ""
	@echo "  ha-up         start the dev Home Assistant container"
	@echo "  ha-down       stop the dev HA container"
	@echo "  ha-restart    restart HA (after Python changes to the integration)"
	@echo "  ha-logs       tail HA logs"
	@echo "  ha-shell      shell into the running HA container"
	@echo "  ha-clean      wipe dev/config (keeps configuration.yaml) — fresh slate"

# ---------- Python testing ----------

$(VENV)/bin/activate:
	$(PY) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-test.txt

setup: $(VENV)/bin/activate

test: setup
	$(PYTEST) tests/

test-watch: setup
	$(PIP) install pytest-watch >/dev/null
	$(VENV)/bin/ptw tests/ -- -q

lint: setup
	$(RUFF) check custom_components tests

format: setup
	$(RUFF) format custom_components tests

# ---------- Docker HA ----------

ha-up:
	@mkdir -p dev/config
	docker compose up -d
	@echo ""
	@echo "Home Assistant starting at http://localhost:8123"
	@echo "  - first start: ~1-2 min, then complete onboarding once"
	@echo "  - after onboarding, trusted_networks auto-logs you in"
	@echo "  - tail logs: make ha-logs"

ha-down:
	docker compose down

ha-restart:
	docker compose restart homeassistant

ha-logs:
	docker compose logs -f homeassistant

ha-shell:
	docker compose exec homeassistant /bin/bash

ha-clean:
	@docker compose down 2>/dev/null || true
	@echo "Wiping dev/config (keeping configuration.yaml)..."
	@find dev/config -mindepth 1 -not -name configuration.yaml -delete
	@echo "Done. Run 'make ha-up' for a fresh HA."

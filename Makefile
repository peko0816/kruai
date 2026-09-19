# KruAI Makefile
# All backend commands run inside the uv-managed venv rooted at backend/.
# Keep target names stable: docs/CODING_STANDARDS.md and CI reference them.

BACKEND := backend
UV := uv
UV_RUN := $(UV) run --directory $(BACKEND)

.PHONY: help sync check lint fmt fmt-check type test selfcheck migrate run bot i18n i18n-strict clean

help:
	@echo "KruAI make targets:"
	@echo "  make sync       install/refresh backend dev deps (uv sync --all-groups)"
	@echo "  make check      lint + format-check + type + test  (must be green to commit)"
	@echo "  make lint       ruff check"
	@echo "  make fmt        ruff format (writes)"
	@echo "  make fmt-check  ruff format --check"
	@echo "  make type       mypy --strict"
	@echo "  make test       pytest"
	@echo "  make selfcheck  verify the configured providers cover what V1 needs"
	@echo "  make migrate    alembic upgrade head (available after A4)"
	@echo "  make run        uvicorn --factory app.main:create_app --reload"
	@echo "  make bot        run the Telegram bot against a running API"
	@echo "  make i18n       report how many translations are still placeholders"
	@echo "  make i18n-strict  fail while any translation is missing (the launch gate)"

sync:
	$(UV) sync --directory $(BACKEND) --all-groups

check: lint fmt-check type test

lint:
	$(UV_RUN) ruff check . ../bot

fmt:
	$(UV_RUN) ruff format . ../bot

fmt-check:
	$(UV_RUN) ruff format --check . ../bot

type:
	$(UV_RUN) mypy

test:
	$(UV_RUN) pytest

# Reads the real .env, so it catches a deployment the test suite cannot see.
# Exits non-zero when a configured provider cannot do its job.
selfcheck:
	$(UV_RUN) python -m app.services.selfcheck

migrate:
	$(UV_RUN) alembic upgrade head

# --factory because app/main.py builds the app in a function: importing that
# module must not read .env or open a connection pool.
run:
	$(UV_RUN) uvicorn --factory app.main:create_app --reload

# `uv run --directory backend` moves the working directory, so the repository
# root has to be on the path for `bot` to import. pytest gets the same thing
# from `pythonpath` in pyproject.toml.
BOT_RUN := PYTHONPATH=$(CURDIR) $(UV_RUN) python

# Needs TELEGRAM_BOT_TOKEN in .env and `make run` already serving the API.
bot:
	$(BOT_RUN) -m bot.main

# Reports how much of the catalogue is still waiting for a translator. Never
# fails on that count: see bot/i18n_check.py on why it is a launch gate.
i18n:
	$(BOT_RUN) -m bot.i18n_check

# The launch gate itself. docs/DEFINITION_OF_DONE.md runs this before M2.
i18n-strict:
	$(BOT_RUN) -m bot.i18n_check --strict

clean:
	rm -rf $(BACKEND)/.venv $(BACKEND)/.mypy_cache $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

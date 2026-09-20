# KruAI Makefile
# All backend commands run inside the uv-managed venv rooted at backend/.
# Keep target names stable: docs/CODING_STANDARDS.md and CI reference them.

BACKEND := backend
UV := uv
UV_RUN := $(UV) run --directory $(BACKEND)

.PHONY: help sync check lint fmt fmt-check type test selfcheck migrate run bot jobs i18n i18n-strict seed seed-strict generate validate review wordlist clean

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
	@echo "  make jobs       run the scheduled jobs once (renewals, reminders, reconciliation)"
	@echo "  make i18n       report how many translations are still placeholders"
	@echo "  make i18n-strict  fail while any translation is missing (the launch gate)"
	@echo "  make seed       validate the content seed files"
	@echo "  make seed-strict  also fail while a Khmer explanation is a placeholder (M1 gate)"
	@echo "  make generate   draft the content for every seeded concept (LLM; fake by default)"
	@echo "  make validate   run the eight content rules over a draft (DRAFT=<path>)"
	@echo "  make review     export the native-speaker review sample (DRAFT=<path>)"
	@echo "  make wordlist   rebuild a level's word list from its transcription table"

sync:
	$(UV) sync --directory $(BACKEND) --all-groups

check: lint fmt-check type test

lint:
	$(UV_RUN) ruff check . ../bot ../pipeline

fmt:
	$(UV_RUN) ruff format . ../bot ../pipeline

fmt-check:
	$(UV_RUN) ruff format --check . ../bot ../pipeline

type:
	$(UV_RUN) mypy

# -n 4 rather than -n auto: the integration suite waits on PostgreSQL round
# trips rather than on CPU, so it parallelises well, but each worker takes a
# scratch database and a Redis logical database and those are not unlimited.
# Override with `make test PYTEST_WORKERS=1` when a failure needs a clean
# single-process run.
PYTEST_WORKERS ?= 4

test:
	$(UV_RUN) pytest -n $(PYTEST_WORKERS) --dist loadfile

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
# root has to be on the path for the sibling packages — `bot` and `pipeline` —
# to import. pytest gets the same thing from `pythonpath` in pyproject.toml.
ROOT_RUN := PYTHONPATH=$(CURDIR) $(UV_RUN) python

# Needs TELEGRAM_BOT_TOKEN in .env and `make run` already serving the API.
bot:
	$(ROOT_RUN) -m bot.main

# The scheduled jobs. No scheduler is wired up yet: run them from cron, from
# RQ, or by hand. Each is idempotent and safe to run more often than needed.
jobs:
	$(UV_RUN) python -m app.workers.subscriptions
	$(UV_RUN) python -m app.workers.payments

# Reports how much of the catalogue is still waiting for a translator. Never
# fails on that count: see bot/i18n_check.py on why it is a launch gate.
i18n:
	$(ROOT_RUN) -m bot.i18n_check

# The launch gate itself. docs/DEFINITION_OF_DONE.md runs this before M2.
i18n-strict:
	$(ROOT_RUN) -m bot.i18n_check --strict

# Validates pipeline/seed/ (BACKLOG E1). The structural half also runs inside
# `make check`, so a malformed seed fails a pull request; this prints the
# corpus and what is still waiting for a Khmer explanation.
seed:
	$(ROOT_RUN) -m pipeline.validate_seed

# The M1 gate: also refuses while any Khmer explanation is a placeholder.
seed-strict:
	$(ROOT_RUN) -m pipeline.validate_seed --strict

# Stage [2] of the pipeline (BACKLOG E3). Free and deterministic while
# LLM_PROVIDER=fake; with a real provider it is billed per token and refuses to
# start without --confirm-spend (docs/DECISIONS.md D-085).
generate:
	$(ROOT_RUN) -m pipeline.generate

# Stage [3] (BACKLOG E4): the eight rules of PRD 6.2 over a generated draft.
# Exits 2 when the level has no word list — rule 1 must not pass by default.
# Absolute: the recipes run with the working directory moved to backend/,
# so a repository-relative path would resolve to the wrong place.
DRAFT ?= $(CURDIR)/pipeline/packs/zh-HSK1.draft.json

validate:
	$(ROOT_RUN) -m pipeline.validate $(DRAFT)

# Stage [4] (BACKLOG E5): draw the native-speaker review sample from a draft.
# Writes a CSV beside the draft and prints what to send with it.
review:
	$(ROOT_RUN) -m pipeline.review_export $(DRAFT)

# Regenerates pipeline/wordlists/<lang>/<level>.txt from the transcription
# table beside it. The list is derived; corrections go into the table.
wordlist:
	$(ROOT_RUN) -m pipeline.build_wordlist

clean:
	rm -rf $(BACKEND)/.venv $(BACKEND)/.mypy_cache $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

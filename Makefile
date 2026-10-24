# KruAI Makefile
# All backend commands run inside the uv-managed venv rooted at backend/.
# Keep target names stable: docs/CODING_STANDARDS.md and CI reference them.

BACKEND := backend
UV := uv
UV_RUN := $(UV) run --directory $(BACKEND)

.PHONY: help sync check lint fmt fmt-check type test selfcheck migrate run bot jobs i18n i18n-strict i18n-export i18n-apply seed seed-strict generate validate repair fixes review m0-scoring m0-tts m0-tts-score build import wordlist clean

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
	@echo "  make i18n-export  write the sheet a translator fills in (LOCALE=km)"
	@echo "  make i18n-apply   merge a filled sheet back (FILE=<path> LOCALE=km)"
	@echo "  make seed       validate the content seed files"
	@echo "  make seed-strict  also fail while a Khmer explanation is a placeholder (M1 gate)"
	@echo "  make generate   draft the content for every seeded concept (LLM; fake by default)"
	@echo "  make fixes      the violations as a sheet to work through"
	@echo "  make repair     re-ask the concepts validate.py rejected (LLM; fake by default)"
	@echo "  make validate   run the eight content rules over a draft (DRAFT=<path>)"
	@echo "  make m0-scoring  M0-1 verification (SCORING_PROVIDER; fake by default)"
	@echo "  make m0-tts      M0-2 blind listening test (export / m0-tts-score)"
	@echo "  make review     export the native-speaker review sample (DRAFT=<path>)"
	@echo "  make build      freeze a draft into a pack with its audio (DRAFT=<path>)"
	@echo "  make import     import a pack into the database (PACK=<path>)"
	@echo "  make wordlist   rebuild a level's word list from its transcription table"

sync:
	$(UV) sync --directory $(BACKEND) --all-groups

check: lint fmt-check type test

lint:
	$(UV_RUN) ruff check . ../bot ../pipeline ../m0

fmt:
	$(UV_RUN) ruff format . ../bot ../pipeline ../m0

fmt-check:
	$(UV_RUN) ruff format --check . ../bot ../pipeline ../m0

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

# Hands the untranslated strings to a translator as a spreadsheet, and takes
# them back. `apply` refuses a translation that drops a {parameter} or that is
# not written in the target script (D-102).
LOCALE ?= km

i18n-export:
	$(ROOT_RUN) -m bot.i18n_handoff export --locale $(LOCALE)

i18n-apply:
	$(ROOT_RUN) -m bot.i18n_handoff apply $(FILE) --locale $(LOCALE)

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

# The same violations as a sheet somebody works through one at a time. These
# are Chinese-side problems; `make review` is the separate Khmer judgement.
FIXES ?= $(CURDIR)/pipeline/packs/zh-HSK1.fixes.csv

fixes:
	$(ROOT_RUN) -m pipeline.validate $(DRAFT) --csv $(FIXES)

# M0-1 (docs/REAL_ENVIRONMENT.md item 3): can a threshold separate a good
# speaker from a real beginner? Runs on FakeScorer for free, which is how it
# was written; a billed provider needs ARGS="--confirm-spend".
M0_MANIFEST ?= $(CURDIR)/m0/samples/manifest.csv

m0-scoring:
	$(ROOT_RUN) -m m0.scoring_check $(M0_MANIFEST) $(ARGS)

# M0-2 (item 5): a blind listening test. Export synthesises one clip per
# passage per candidate voice under names that do not say which; score
# un-blinds the filled sheets. The key file is written outside the clip
# folder so sending the folder cannot send the key with it.
M0_CORPUS ?= $(CURDIR)/m0/samples/khmer-corpus.csv
M0_CANDIDATES ?= fake

m0-tts:
	$(ROOT_RUN) -m m0.tts_blind export $(M0_CORPUS) --candidates $(M0_CANDIDATES) $(ARGS)

m0-tts-score:
	$(ROOT_RUN) -m m0.tts_blind score $(SHEETS) $(ARGS)

# Stage [4b]: re-ask only the concepts validate.py rejected, with their own
# violations in the prompt, keeping whichever version of each is better.
ROUNDS ?= 1

repair:
	$(ROOT_RUN) -m pipeline.repair --rounds $(ROUNDS)

# Stage [5] (BACKLOG E6): freeze a draft into a pack and synthesise its audio.
# Refuses a draft validate.py rejects, a billed TTS provider without
# --confirm-spend, and --with-video (PRD 14: not before S2).
build:
	$(ROOT_RUN) -m pipeline.build_pack $(DRAFT)

# Stage [4] (BACKLOG E5): draw the native-speaker review sample from a draft.
# Writes a CSV beside the draft and prints what to send with it.
review:
	$(ROOT_RUN) -m pipeline.review_export $(DRAFT)

# Stage [6] (BACKLOG E7): put a pack into the database and publish its audio.
# Refuses an unvalidated pack, an edited one, and a version already imported.
PACK ?= $(CURDIR)/pipeline/packs/zh-HSK1.pack.json

import:
	$(ROOT_RUN) -m pipeline.import_pack $(PACK)

# Regenerates pipeline/wordlists/<lang>/<level>.txt from the transcription
# table beside it. The list is derived; corrections go into the table.
wordlist:
	$(ROOT_RUN) -m pipeline.build_wordlist

clean:
	rm -rf $(BACKEND)/.venv $(BACKEND)/.mypy_cache $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

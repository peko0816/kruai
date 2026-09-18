# KruAI Makefile
# All backend commands run inside the uv-managed venv rooted at backend/.
# Keep target names stable: docs/CODING_STANDARDS.md and CI reference them.

BACKEND := backend
UV := uv
UV_RUN := $(UV) run --directory $(BACKEND)

.PHONY: help sync check lint fmt fmt-check type test selfcheck migrate run clean

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

sync:
	$(UV) sync --directory $(BACKEND) --all-groups

check: lint fmt-check type test

lint:
	$(UV_RUN) ruff check .

fmt:
	$(UV_RUN) ruff format .

fmt-check:
	$(UV_RUN) ruff format --check .

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

clean:
	rm -rf $(BACKEND)/.venv $(BACKEND)/.mypy_cache $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

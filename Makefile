# KruAI Makefile
# All backend commands run inside the uv-managed venv rooted at backend/.
# Keep target names stable: docs/CODING_STANDARDS.md and CI reference them.

BACKEND := backend
UV := uv
UV_RUN := $(UV) run --directory $(BACKEND)

.PHONY: help sync check lint fmt fmt-check type test migrate run clean

help:
	@echo "KruAI make targets:"
	@echo "  make sync       install/refresh backend dev deps (uv sync --all-groups)"
	@echo "  make check      lint + format-check + type + test  (must be green to commit)"
	@echo "  make lint       ruff check"
	@echo "  make fmt        ruff format (writes)"
	@echo "  make fmt-check  ruff format --check"
	@echo "  make type       mypy --strict"
	@echo "  make test       pytest"
	@echo "  make migrate    alembic upgrade head (available after A4)"
	@echo "  make run        uvicorn app.main:app --reload (available after api scaffolding)"

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

migrate:
	$(UV_RUN) alembic upgrade head

run:
	$(UV_RUN) uvicorn app.main:app --reload

clean:
	rm -rf $(BACKEND)/.venv $(BACKEND)/.mypy_cache $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

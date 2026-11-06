# One image for every KruAI process: the API, the bot, migrations and pack
# imports. Which one runs is the command (deploy/compose.yml). D-144.

FROM ghcr.io/astral-sh/uv:0.11.7 AS uv

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    # The layout `make` uses: backend/ is the working directory (the `app`
    # package), the repository root holds the sibling packages bot/ and pipeline/.
    PYTHONPATH=/srv/kruai:/srv/kruai/backend

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /srv/kruai/backend

# Dependencies first, from the lockfile alone, so a code change reuses this layer.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/ /srv/kruai/backend/
COPY bot/ /srv/kruai/bot/
COPY pipeline/ /srv/kruai/pipeline/
COPY locales/ /srv/kruai/locales/

RUN useradd --system --uid 10001 --no-create-home kruai
USER kruai

EXPOSE 8000
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000"]

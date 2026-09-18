"""The ASGI application.

Built by a factory, not assembled at import time, so that importing this module
neither reads .env nor opens a socket. ``make run`` therefore uses uvicorn's
``--factory`` form::

    uvicorn --factory app.main:create_app --reload

Two things happen at startup, in this order:

  1. the provider self-check (BACKLOG B5), which refuses to boot a deployment
     whose configured providers cannot cover what V1 needs. Discovering that at
     boot is the entire point — the alternative is discovering it when a
     learner presses record;
  2. the database engine, whose pool is owned by the lifespan and disposed with
     it. A pool created at import time would outlive nothing and be shared by
     every test that imported the module.

AppError is translated to HTTP here, in one place. Services raise business
errors and know nothing about status codes (CODING_STANDARDS section 5.2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1 import router as v1_router
from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_session_factory
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.services.selfcheck import check_provider_configuration

log = get_logger(__name__)

API_V1_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Wire the application. Pass settings to override the environment."""
    resolved = settings or get_settings()
    configure_logging(log_level=resolved.log_level, json_output=resolved.env != "dev")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        check_provider_configuration(resolved)
        engine = create_engine(resolved)
        app.state.session_factory = create_session_factory(engine)
        log.info("app.started", env=resolved.env)
        try:
            yield
        finally:
            await engine.dispose()
            log.info("app.stopped")

    app = FastAPI(
        title="KruAI",
        version="0.1.0",
        lifespan=lifespan,
        # No interactive docs outside development: the schema names every
        # endpoint and every field, and there is no reason to publish that.
        docs_url=None if resolved.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if resolved.is_production else "/openapi.json",
    )
    app.state.settings = resolved
    app.include_router(v1_router, prefix=API_V1_PREFIX)
    app.add_exception_handler(AppError, app_error_handler)
    return app


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render an AppError as its declared status and machine-readable code.

    ``exc`` is typed as Exception because that is the signature Starlette's
    handler registry declares; narrowing it would need a type: ignore, and the
    guard below says the same thing at runtime without one.

    Only ``code`` and ``message`` go out. ``context`` is for the log — it is
    where callers put the detail that would tell an attacker which check they
    failed.
    """
    if not isinstance(exc, AppError):  # pragma: no cover - registry contract
        raise exc
    log.warning(
        "api.app_error",
        code=exc.code,
        http_status=exc.http_status,
        path=request.url.path,
        **exc.context,
    )
    headers = {"WWW-Authenticate": "Bearer"} if exc.http_status == 401 else None
    return JSONResponse(
        status_code=exc.http_status,
        content={"code": exc.code, "message": exc.message},
        headers=headers,
    )

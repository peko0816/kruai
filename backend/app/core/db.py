"""Async database access.

The same DATABASE_URL serves both this and alembic. ``postgresql+psycopg://``
resolves to psycopg3, whose dialect is sync for the migration tool and async
here, so there is no second connection string to keep in step.

No module-level engine. An engine owns a connection pool, and creating one at
import time would open sockets during test collection and tie every caller to
one configuration. The application builds one at startup and passes it down;
tests build their own against a scratch database.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Connection pool for this configuration.

    ``echo`` follows LOG_LEVEL rather than a separate switch: someone who turned
    on DEBUG wants to see the SQL.
    """
    return create_async_engine(
        settings.database_url,
        echo=settings.log_level == "DEBUG",
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions that do not expire attributes on commit.

    ``expire_on_commit=False`` because a handler routinely reads fields off an
    object after committing; the default would issue a fresh SELECT for each,
    and under async that lazy refresh raises MissingGreenlet rather than just
    being slow.
    """
    return async_sessionmaker(engine, expire_on_commit=False)

"""Request-scoped dependencies.

Settings and the session factory are built once at startup and parked on
``app.state``; these read them back. Nothing here constructs either, because a
dependency that built its own engine would open a connection pool per request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, closed whatever happens.

    Handlers commit their own work. Committing here instead would make every
    handler's transaction end successfully by default, including the ones that
    raised on the way out.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

"""Request-scoped dependencies.

Settings and the session factory are built once at startup and parked on
``app.state``; these read them back. Nothing here constructs either, because a
dependency that built its own engine would open a connection pool per request.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import AuthenticationFailed
from app.core.security import decode_access_token

#: ``auto_error=False`` so a missing or malformed header arrives here rather
#: than as Starlette's own 403 — every authentication failure should leave by
#: the same door, with the same code and the same log line.
_bearer = HTTPBearer(auto_error=False, description="Token from POST /auth/telegram")


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """For the two domain services that open their own session (D-018, D-023)."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, closed whatever happens.

    Handlers commit their own work. Committing here instead would make every
    handler's transaction end successfully by default, including the ones that
    raised on the way out.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


def get_current_user_id(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> uuid.UUID:
    """The caller's user id, proven by the bearer token.

    The token is not trusted to say anything else about them (D-025): plan,
    quota and locale are read per request from the database, so a token issued
    before an upgrade — or a downgrade — cannot outrank it.

    Raises:
        AuthenticationFailed: no bearer token, or one that does not verify.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationFailed(reason="bearer_token_missing")
    return decode_access_token(credentials.credentials, settings=get_settings(request))


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
CurrentUserDep = Annotated[uuid.UUID, Depends(get_current_user_id)]

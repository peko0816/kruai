"""POST /api/v1/auth/telegram — the only way into the system.

Every other endpoint will take the token this one issues. The verification
itself lives in core/security.py; what is here is the part that needs a
database: turning a proven Telegram account into a row we can hang progress,
quota and payments off.

Provisioning is idempotent by construction. A learner opens the Mini App
several times a day and every one of those calls lands here, so each of the
three inserts is written so that arriving second is not an error — the second
caller gets the same user id, not a duplicate account and not a 500.

The three rows are created together on purpose. ``entitlements`` in particular
has no lazy-creation path anywhere in the system: quota.py treats a missing row
as a provisioning failure rather than as an empty allowance (D-018), which is
only safe if every account gets one at birth.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SessionDep, SettingsDep
from app.core.config import Settings
from app.core.errors import AuthenticationFailed
from app.core.logging import get_logger
from app.core.security import (
    TelegramIdentity,
    issue_access_token,
    verify_init_data,
)
from app.models.commerce import Entitlement
from app.models.users import User, UserProfile
from app.services.entitlements.reset import next_reset_at

log = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class TelegramAuthRequest(BaseModel):
    """The Mini App's initData, forwarded byte for byte.

    The client must not parse it first. Its fields are only worth anything
    while the signature still covers them (CODING_STANDARDS section 12).
    """

    init_data: str = Field(min_length=1)


class AccessTokenResponse(BaseModel):
    """An OAuth-shaped bearer token, so clients can use their usual plumbing."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


@router.post("/telegram", status_code=status.HTTP_200_OK)
async def authenticate_telegram(
    payload: TelegramAuthRequest, session: SessionDep, settings: SettingsDep
) -> AccessTokenResponse:
    """Exchange verified initData for a session token, creating the account."""
    now = datetime.datetime.now(datetime.UTC)
    identity = verify_init_data(
        payload.init_data,
        bot_token=settings.telegram_bot_token.get_secret_value(),
        now=now,
        max_age_seconds=settings.telegram_init_data_max_age_seconds,
    )
    user_id = await provision_account(session, identity=identity, settings=settings, now=now)
    token = issue_access_token(user_id, settings=settings, now=now)

    log.info(
        "auth.telegram.authenticated",
        user_id=str(user_id),
        locale=identity.locale,
        # How stale the initData was, which is the number to look at if the
        # freshness window ever has to be tuned.
        init_data_age_seconds=int((now - identity.auth_date).total_seconds()),
    )
    return AccessTokenResponse(access_token=token.token, expires_in=token.expires_in)


async def provision_account(
    session: AsyncSession,
    *,
    identity: TelegramIdentity,
    settings: Settings,
    now: datetime.datetime,
) -> uuid.UUID:
    """The user id behind this Telegram account, creating the account if new.

    Raises:
        AuthenticationFailed: the account is soft-deleted. Re-authenticating
            must not undelete it, so the sign-in is refused instead.
    """
    user_id, deleted_at = await _upsert_user(session, identity=identity, now=now)
    if deleted_at is not None:
        # Roll back rather than commit: the upsert already touched
        # last_active_at, and a deleted account should not look active.
        await session.rollback()
        log.warning("auth.telegram.refused", user_id=str(user_id), reason="account_deleted")
        raise AuthenticationFailed(reason="account_deleted", user_id=str(user_id))

    timezone = await _ensure_profile(session, user_id=user_id)
    created = await _ensure_entitlements(
        session, user_id=user_id, timezone=timezone, settings=settings, now=now
    )
    await session.commit()

    if created:
        log.info("auth.telegram.provisioned", user_id=str(user_id), timezone=timezone)
    return user_id


async def _upsert_user(
    session: AsyncSession, *, identity: TelegramIdentity, now: datetime.datetime
) -> tuple[uuid.UUID, datetime.datetime | None]:
    """Insert or touch the users row, returning its id and deletion state.

    DO UPDATE rather than DO NOTHING because DO NOTHING returns no row on
    conflict, and the returning user is the common case — we would then need a
    second SELECT on every single sign-in.

    ``locale`` is set on insert only. Telegram's language_code is a reasonable
    first guess, not a standing instruction: a learner who later picks a
    different interface language would have that choice reverted on their next
    sign-in.
    """
    statement = (
        pg_insert(User)
        .values(
            telegram_id=identity.telegram_id,
            locale=identity.locale,
            last_active_at=now,
        )
        .on_conflict_do_update(index_elements=["telegram_id"], set_={"last_active_at": now})
        .returning(User.id, User.deleted_at)
    )
    row = (await session.execute(statement)).one()
    user_id: uuid.UUID = row[0]
    deleted_at: datetime.datetime | None = row[1]
    return user_id, deleted_at


async def _ensure_profile(session: AsyncSession, *, user_id: uuid.UUID) -> str:
    """Create the profile if absent; return the learner's timezone either way.

    Only ``user_id`` is supplied so that every other column takes its database
    default. Spelling 'Asia/Phnom_Penh' here would be a second place to change
    it, and the one that ends up disagreeing with DATA_MODEL.sql.
    """
    statement = (
        pg_insert(UserProfile)
        .values(user_id=user_id)
        .on_conflict_do_nothing(index_elements=["user_id"])
        .returning(UserProfile.timezone)
    )
    inserted: str | None = (await session.execute(statement)).scalar_one_or_none()
    if inserted is not None:
        return inserted

    existing = await session.execute(
        sa.select(UserProfile.timezone).where(UserProfile.user_id == user_id)
    )
    return existing.scalar_one()


async def _ensure_entitlements(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    timezone: str,
    settings: Settings,
    now: datetime.datetime,
) -> bool:
    """Create the allowance row if absent. True when this call created it.

    ``reset_at`` has no database default — it depends on the learner's own
    timezone — so it is computed here with the same function the daily reset
    job uses. A returning learner's row is left alone: their counters and their
    reset schedule are already in flight.
    """
    statement = (
        pg_insert(Entitlement)
        .values(
            user_id=user_id,
            reset_at=next_reset_at(now, timezone=timezone, settings=settings),
        )
        .on_conflict_do_nothing(index_elements=["user_id"])
        .returning(Entitlement.user_id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None

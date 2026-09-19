"""Where each learner is, kept in Redis.

A conversation that forgets itself on deploy is a conversation the learner has
to restart, and the M2 checklist asks for state that survives a restart. Redis
rather than a table because this is genuinely ephemeral — a half-finished
lesson is worth a day, not forever — and expiry is one argument rather than a
cleanup job.

**This is the first thing in the repository that connects to Redis.** Carrying
constraint L-2: the CI workflow had no redis service, so a test that needed one
would have skipped or failed quietly. The service is added in the same change
as this file.

Nothing here decides anything about a lesson; it stores and returns the
dataclass that bot/flow.py computed.
"""

from __future__ import annotations

import json
import uuid
from typing import Final

from redis.asyncio import Redis

from bot.flow import Session

#: One namespace so a shared Redis stays legible, and so a flush of bot state
#: cannot take RQ's queues with it.
KEY_PREFIX: Final = "kruai:bot:session:"


def key_for(telegram_id: int) -> str:
    return f"{KEY_PREFIX}{telegram_id}"


class SessionStore:
    """Reads and writes one learner's place in a lesson."""

    def __init__(self, redis: Redis, *, ttl_seconds: int) -> None:
        # redis-py ships its own types since 5.0 and its Redis is not generic —
        # the old types-redis stubs made it Redis[bytes] and disagreed with the
        # runtime about aclose(), so this project dropped them.
        self._redis = redis
        self._ttl = ttl_seconds

    async def load(self, telegram_id: int) -> Session | None:
        """The stored session, or None if there is none.

        A stored value that cannot be read back — an old shape after a deploy,
        a truncated write — is treated as no session rather than as an error.
        The learner starts the lesson again, which is recoverable; a handler
        that raises on every message is not.
        """
        raw = await self._redis.get(key_for(telegram_id))
        if raw is None:
            return None
        try:
            stored = json.loads(raw)
            return Session(
                lesson_id=uuid.UUID(stored["lesson_id"]),
                step_index=int(stored["step_index"]),
                attempts_used=int(stored["attempts_used"]),
            )
        except (ValueError, TypeError, KeyError):
            await self.clear(telegram_id)
            return None

    async def save(self, telegram_id: int, session: Session) -> None:
        """Store the session, refreshing its expiry.

        The TTL is reset on every write, so a lesson someone is actively
        working through never expires under them; one abandoned mid-way goes
        away on its own.
        """
        await self._redis.set(
            key_for(telegram_id),
            json.dumps(
                {
                    "lesson_id": str(session.lesson_id),
                    "step_index": session.step_index,
                    "attempts_used": session.attempts_used,
                }
            ),
            ex=self._ttl,
        )

    async def clear(self, telegram_id: int) -> None:
        await self._redis.delete(key_for(telegram_id))


__all__ = ["KEY_PREFIX", "SessionStore", "key_for"]

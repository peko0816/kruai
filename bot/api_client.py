"""The bot's only way to reach the system: the public API.

ARCHITECTURE section 1 puts the bot in the entry layer, which talks to the API
over HTTP and nothing else. It would be shorter to import the services — they
are in the same repository — and that is exactly why the rule exists: the first
import is where the entry layer starts making business decisions, and nobody
notices until two clients disagree about what a passing score is.

**Authentication.** The Mini App forwards initData signed by Telegram; a chat
bot never receives one. Rather than a second authentication mechanism, the bot
signs an initData of its own with the bot token — the same key Telegram signs
with, which this process already holds because it is the bot. The API verifies
it the same way it verifies a Mini App's (docs/DECISIONS.md D-048).

Tokens are cached per learner until shortly before they expire. Re-signing on
every message would be correct and would also mean an authentication round trip
per voice note.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

import httpx

from bot.flow import Lesson, Step

#: Telegram's derivation constant. Fixed by their protocol (see
#: app/core/security.py, which checks the other side of this).
_SECRET_KEY_SALT: Final = b"WebAppData"

#: Re-authenticate this long before the token actually expires, so a request
#: cannot be issued with a token that dies in flight.
_REFRESH_MARGIN_SECONDS: Final = 60


class ApiError(RuntimeError):
    """The API answered with something this client cannot use."""

    def __init__(self, status: int, code: str | None, detail: str = "") -> None:
        self.status = status
        self.code = code
        super().__init__(f"API returned {status} ({code or 'no code'}) {detail}".strip())


@dataclass
class _CachedToken:
    token: str
    expires_at: datetime.datetime


def sign_init_data(
    *, bot_token: str, telegram_id: int, language_code: str | None, now: datetime.datetime
) -> str:
    """Build the initData string a Mini App would have sent for this learner.

    The chain is sorted and joined exactly as Telegram builds it, because the
    server checks it exactly as it checks Telegram's — that symmetry is the
    whole point, and an integration test runs one through the other.
    """
    user: dict[str, Any] = {"id": telegram_id}
    if language_code:
        user["language_code"] = language_code

    fields = {
        "auth_date": str(int(now.timestamp())),
        "user": json.dumps(user, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(_SECRET_KEY_SALT, bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


class KruaiApi:
    """A typed view of the endpoints the bot uses."""

    def __init__(self, client: httpx.AsyncClient, *, bot_token: str) -> None:
        self._client = client
        self._bot_token = bot_token
        self._tokens: dict[int, _CachedToken] = {}

    # ------------------------------------------------------------------ auth

    async def token_for(self, telegram_id: int, *, language_code: str | None = None) -> str:
        """A session token for this learner, minted on first use and cached."""
        now = datetime.datetime.now(datetime.UTC)
        cached = self._tokens.get(telegram_id)
        if cached is not None and cached.expires_at > now:
            return cached.token

        init_data = sign_init_data(
            bot_token=self._bot_token,
            telegram_id=telegram_id,
            language_code=language_code,
            now=now,
        )
        body = await self._post("/api/v1/auth/telegram", json={"init_data": init_data})
        token = str(body["access_token"])
        lifetime = int(body["expires_in"]) - _REFRESH_MARGIN_SECONDS
        self._tokens[telegram_id] = _CachedToken(
            token=token,
            expires_at=now + datetime.timedelta(seconds=max(lifetime, 0)),
        )
        return token

    def forget(self, telegram_id: int) -> None:
        """Drop a cached token, so the next call re-authenticates."""
        self._tokens.pop(telegram_id, None)

    # -------------------------------------------------------------- content

    async def first_lesson_id(self, token: str) -> uuid.UUID | None:
        """The opening lesson of the first course on offer.

        A placeholder for choosing: the catalogue is one HSK1 course at M2, and
        picking up where a learner left off needs progress the API does not
        expose yet. Deliberately not guessed at here — a wrong guess would be a
        business decision made in the entry layer.
        """
        courses = await self._get("/api/v1/courses", token=token)
        if not courses:
            return None
        lessons = await self._get(f"/api/v1/courses/{courses[0]['id']}/lessons", token=token)
        if not lessons:
            return None
        return uuid.UUID(lessons[0]["id"])

    async def lesson(self, token: str, lesson_id: uuid.UUID) -> Lesson:
        """One lesson, already shaped for the conversation.

        The media choice is the server's (D2): the bot reads ``primary_url``
        and never looks at plan or item type to decide what to play.
        """
        body = await self._get(f"/api/v1/lessons/{lesson_id}", token=token)
        return Lesson(
            lesson_id=uuid.UUID(body["id"]),
            title=body["title_km"],
            steps=tuple(_step_from(item) for item in body["items"]),
        )

    # -------------------------------------------------------------- learning

    async def submit_attempt(
        self, token: str, *, lesson_item_id: uuid.UUID, audio: bytes, filename: str
    ) -> dict[str, Any]:
        """Send one recording. Raises ApiError for quota and provider failures.

        The caller maps those to what the learner is told; this layer does not
        decide that a 402 means "buy something" or that a 503 means "try again".
        """
        response = await self._client.post(
            "/api/v1/attempts",
            headers={"Authorization": f"Bearer {token}"},
            data={"lesson_item_id": str(lesson_item_id)},
            files={"audio": (filename, audio, "audio/ogg")},
        )
        body: dict[str, Any] = _unwrap(response)
        return body

    async def start_lesson(self, token: str, lesson_id: uuid.UUID) -> dict[str, Any]:
        """Begin a lesson. Raises ApiError with quota.insufficient when the
        day's tasks are spent — the bot turns that into a sentence, it does not
        decide it."""
        body: dict[str, Any] = await self._post(f"/api/v1/lessons/{lesson_id}/start", token=token)
        return body

    async def complete_lesson(self, token: str, lesson_id: uuid.UUID) -> dict[str, Any]:
        body: dict[str, Any] = await self._post(
            f"/api/v1/lessons/{lesson_id}/complete", token=token
        )
        return body

    async def entitlements(self, token: str) -> dict[str, Any]:
        body: dict[str, Any] = await self._get("/api/v1/me/entitlements", token=token)
        return body

    # ------------------------------------------------------------- plumbing

    async def _get(self, path: str, *, token: str | None = None) -> Any:
        return _unwrap(await self._client.get(path, headers=_auth(token)))

    async def _post(
        self, path: str, *, token: str | None = None, json: dict[str, Any] | None = None
    ) -> Any:
        return _unwrap(await self._client.post(path, headers=_auth(token), json=json))


def _auth(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _unwrap(response: httpx.Response) -> Any:
    """The body, or an ApiError carrying the machine-readable code.

    The API's error contract is ``{"code": ..., "message": ...}`` and the code
    is what drives the reply a learner sees, so it is kept rather than
    flattened into a status number.
    """
    if response.is_success:
        return response.json()

    code: str | None = None
    try:
        payload = response.json()
        if isinstance(payload, dict):
            code = payload.get("code")
    except ValueError:
        pass
    raise ApiError(response.status_code, code, response.text[:200])


def _step_from(item: dict[str, Any]) -> Step:
    """One lesson item as the conversation needs it.

    ``text`` comes out of the pack's payload, which is content rather than
    schema — an item with nothing sayable in it still has to render, so the
    known keys are tried in order and an empty string is better than a
    traceback in front of a learner.
    """
    payload = item.get("payload") or {}
    media = item.get("media") or {}
    text = payload.get("target_text") or payload.get("text") or payload.get("prompt") or ""
    return Step(
        item_id=uuid.UUID(item["id"]),
        item_type=item["item_type"],
        text=str(text),
        audio_url=media.get("primary_url"),
        video_url=media.get("video_url"),
        disclaimer_key=media.get("disclaimer_key"),
    )


__all__ = ["ApiError", "KruaiApi", "sign_init_data"]

"""Lesson recordings reach Telegram as uploads, not as links (BACKLOG Q9, D-143).

Telegram fetches a file given by URL itself, and for a voice note it then
insists on ``audio/ogg`` under 1 MB (Bot API, "Sending files"). The packs are
MP3, so every recording sent by link would have been refused. An uploaded MP3
is accepted as a voice note, and each upload hands back a ``file_id`` that
sends the same clip again without uploading it.

When Telegram refuses a voice note anyway, the clip goes as an ordinary audio
message — a player rather than a bubble, but the learner hears it. Two
refusals are told apart:

* the learner has turned voice messages off (``VOICE_MESSAGES_FORBIDDEN``):
  their choice, so only this send falls back;
* anything else is taken to be the clip itself, and that clip is sent as audio
  from then on rather than refused for every learner in turn.

When the recording cannot be fetched or sent at all, the text goes alone
(ARCHITECTURE 5: audio degrades to text, never to an error).
"""

from __future__ import annotations

from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from telegram.error import BadRequest

from app.core.logging import get_logger

log = get_logger(__name__)

#: Telegram's limit on a media caption. A longer text goes as its own message
#: first and the recording follows without one.
CAPTION_LIMIT: Final = 1024

#: Telegram's error when the recipient has restricted voice messages.
_VOICE_FORBIDDEN: Final = "voice_messages_forbidden"

Kind = Literal["voice", "audio"]


class Replies(Protocol):
    """The three ways of answering a message used here (``telegram.Message``)."""

    async def reply_text(self, text: str) -> Any: ...

    async def reply_voice(
        self, voice: Any, *, caption: str | None = ..., filename: str | None = ...
    ) -> Any: ...

    async def reply_audio(
        self, audio: Any, *, caption: str | None = ..., filename: str | None = ...
    ) -> Any: ...


class AudioSender:
    """Sends a text with its recording, uploading each clip once."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        #: (url, kind) -> the file_id Telegram gave that clip when sent as that kind.
        self._file_ids: dict[tuple[str, Kind], str] = {}
        #: Clips Telegram would not take as a voice note.
        self._audio_only: set[str] = set()

    async def send(self, message: Replies, *, text: str, audio_url: str) -> None:
        caption: str | None = text
        if len(text) > CAPTION_LIMIT:
            await message.reply_text(text)
            caption = None

        clip: bytes | None = None
        kinds: tuple[Kind, ...] = (
            ("audio",) if audio_url in self._audio_only else ("voice", "audio")
        )
        for kind in kinds:
            file_id = self._file_ids.get((audio_url, kind))
            if file_id is None and clip is None:
                clip = await self._fetch(audio_url)
                if clip is None:
                    break
            try:
                reply = await self._reply(
                    message, kind, file_id or clip, caption=caption, filename=_filename(audio_url)
                )
            except BadRequest as error:
                log.warning("bot.audio_refused", url=audio_url, kind=kind, reason=str(error))
                if kind == "voice" and _VOICE_FORBIDDEN not in str(error).lower():
                    self._audio_only.add(audio_url)
                continue
            if file_id is None:
                remembered = _file_id(reply, kind)
                if remembered:
                    self._file_ids[(audio_url, kind)] = remembered
            return

        if caption is not None:
            await message.reply_text(text)

    async def _fetch(self, url: str) -> bytes | None:
        try:
            response = await self._http.get(url)
        except httpx.HTTPError as error:
            log.warning("bot.audio_unreachable", url=url, error=type(error).__name__)
            return None
        if response.status_code != httpx.codes.OK or not response.content:
            log.warning("bot.audio_unreachable", url=url, status=response.status_code)
            return None
        return response.content

    @staticmethod
    async def _reply(
        message: Replies, kind: Kind, payload: Any, *, caption: str | None, filename: str
    ) -> Any:
        if kind == "voice":
            return await message.reply_voice(payload, caption=caption, filename=filename)
        return await message.reply_audio(payload, caption=caption, filename=filename)


def _filename(url: str) -> str:
    return urlsplit(url).path.rsplit("/", 1)[-1] or "clip.mp3"


def _file_id(reply: Any, kind: Kind) -> str | None:
    """The file_id Telegram gave the upload; None if the reply carries none."""
    file_id = getattr(getattr(reply, kind, None), "file_id", None)
    return file_id if isinstance(file_id, str) and file_id else None


__all__ = ["CAPTION_LIMIT", "AudioSender"]

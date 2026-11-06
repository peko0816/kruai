"""Lesson recordings are uploaded to Telegram, once each (bot/audio.py, Q9, D-143).

No Telegram here: a recording stand-in for ``telegram.Message`` keeps what was
sent, and the media server is an httpx MockTransport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from telegram.error import BadRequest

from bot.audio import CAPTION_LIMIT, AudioSender

URL = "https://media.example/zh/ab/abcdef.mp3"
CLIP = b"ID3" + bytes(100)


@dataclass
class Chat:
    """What a learner would see, in order. Refuses whatever it is told to."""

    refuse_voice: str | None = None
    refuse_audio: str | None = None
    sent: list[tuple[str, Any, str | None]] = field(default_factory=list)
    uploads: int = 0

    async def reply_text(self, text: str) -> Any:
        self.sent.append(("text", None, text))

    async def reply_voice(
        self, voice: Any, *, caption: str | None = None, filename: str | None = None
    ) -> Any:
        if self.refuse_voice:
            raise BadRequest(self.refuse_voice)
        return self._keep("voice", voice, caption)

    async def reply_audio(
        self, audio: Any, *, caption: str | None = None, filename: str | None = None
    ) -> Any:
        if self.refuse_audio:
            raise BadRequest(self.refuse_audio)
        return self._keep("audio", audio, caption)

    def _keep(self, kind: str, payload: Any, caption: str | None) -> Any:
        self.sent.append((kind, payload, caption))
        if isinstance(payload, bytes):
            self.uploads += 1
        return SimpleNamespace(**{kind: SimpleNamespace(file_id=f"{kind}-id")})


class Media:
    """The media server: answers with the clip, counting requests."""

    def __init__(
        self, response: httpx.Response | None = None, *, error: Exception | None = None
    ) -> None:
        self.requests = 0
        self._response = response or httpx.Response(200, content=CLIP)
        self._error = error

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self._error:
            raise self._error
        return self._response

    def sender(self) -> AudioSender:
        return AudioSender(httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


async def test_a_recording_is_uploaded_as_a_voice_note_not_linked() -> None:
    """By link Telegram takes only OGG for a voice note; the packs are MP3."""
    chat = Chat()

    await Media().sender().send(chat, text="听", audio_url=URL)

    assert chat.sent == [("voice", CLIP, "听")]


async def test_a_clip_is_uploaded_once_then_sent_by_file_id() -> None:
    media, first, second = Media(), Chat(), Chat()
    sender = media.sender()

    await sender.send(first, text="a", audio_url=URL)
    await sender.send(second, text="b", audio_url=URL)

    assert second.sent == [("voice", "voice-id", "b")]
    assert media.requests == 1
    assert first.uploads + second.uploads == 1


async def test_a_clip_telegram_will_not_take_as_voice_goes_as_audio_from_then_on() -> None:
    media = Media()
    sender = media.sender()
    refusing, later = Chat(refuse_voice="Wrong file type"), Chat()

    await sender.send(refusing, text="a", audio_url=URL)
    await sender.send(later, text="b", audio_url=URL)

    assert refusing.sent == [("audio", CLIP, "a")]
    assert later.sent == [("audio", "audio-id", "b")], "not refused again for every learner"
    assert media.requests == 1


async def test_a_learner_who_turned_voice_off_gets_audio_and_nobody_else_does() -> None:
    sender = Media().sender()
    private = Chat(refuse_voice="Bad Request: VOICE_MESSAGES_FORBIDDEN")
    everyone = Chat()

    await sender.send(everyone, text="a", audio_url=URL)
    await sender.send(private, text="b", audio_url=URL)
    await sender.send(Chat(), text="c", audio_url=URL)
    after = Chat()
    await sender.send(after, text="d", audio_url=URL)

    assert private.sent == [("audio", CLIP, "b")], (
        "the cached voice file_id was refused; uploaded as audio"
    )
    assert after.sent == [("voice", "voice-id", "d")]


@pytest.mark.parametrize(
    "media",
    [
        Media(httpx.Response(404, content=b"<html>Not Found</html>")),
        Media(httpx.Response(200, content=b"")),
        Media(error=httpx.ConnectError("refused")),
    ],
    ids=["missing", "empty", "unreachable"],
)
async def test_a_recording_that_cannot_be_fetched_leaves_the_text(media: Media) -> None:
    """ARCHITECTURE 5: audio degrades to text, never to an error — and the
    server is asked once, not once per way of sending."""
    chat = Chat()

    await media.sender().send(chat, text="听", audio_url=URL)

    assert chat.sent == [("text", None, "听")]
    assert media.requests == 1


async def test_an_unset_media_base_url_leaves_the_text() -> None:
    """PUBLIC_MEDIA_BASE_URL empty at import leaves relative URLs in the database."""
    chat = Chat()
    sender = AudioSender(httpx.AsyncClient())

    await sender.send(chat, text="听", audio_url="/zh/ab/abcdef.mp3")

    assert chat.sent == [("text", None, "听")]


async def test_refused_as_both_leaves_the_text() -> None:
    chat = Chat(refuse_voice="Wrong file type", refuse_audio="Wrong file type")

    await Media().sender().send(chat, text="听", audio_url=URL)

    assert chat.sent == [("text", None, "听")]


async def test_a_text_too_long_for_a_caption_goes_first_on_its_own() -> None:
    chat = Chat()
    text = "ក" * (CAPTION_LIMIT + 1)

    await Media().sender().send(chat, text=text, audio_url=URL)

    assert chat.sent == [("text", None, text), ("voice", CLIP, None)]


async def test_a_text_that_fits_stays_the_caption() -> None:
    chat = Chat()
    text = "ក" * CAPTION_LIMIT

    await Media().sender().send(chat, text=text, audio_url=URL)

    assert chat.sent == [("voice", CLIP, text)]


async def test_a_long_text_is_not_sent_twice_when_the_audio_fails() -> None:
    chat = Chat()
    text = "ក" * (CAPTION_LIMIT + 1)

    await Media(httpx.Response(404)).sender().send(chat, text=text, audio_url=URL)

    assert chat.sent == [("text", None, text)]


async def test_the_bot_sends_lesson_audio_through_the_uploader() -> None:
    """The wiring in bot/handlers.py: a message with audio goes through
    AudioSender, one without goes as text."""
    from bot.handlers import Outgoing, _send

    chat, media = Chat(), Media()
    update = SimpleNamespace(effective_message=chat)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"audio": media.sender()}))

    await _send(update, context, [Outgoing(text="讲解"), Outgoing(text="听", audio_url=URL)])  # type: ignore[arg-type]

    assert chat.sent == [("text", None, "讲解"), ("voice", CLIP, "听")]

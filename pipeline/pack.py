"""A content pack: the frozen form of a level, with its media (BACKLOG E6).

A draft is what the model wrote; a pack is what ships. The difference is that a
pack is closed — every piece of fixed text has an audio file beside it, every
file has a hash of the text it was made from, and the whole thing has a
checksum. PRD 6.3: all fixed text is synthesised at build time and the runtime
only ever reads a URL.

``source_text_hash`` is the load-bearing field (DATA_MODEL.sql names it). It is
taken over the text *and* the voice and speaking rate, so that changing any of
the three makes the existing audio visibly stale rather than silently wrong —
the failure it prevents is a pack that looks right and sounds like the previous
draft.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from pipeline.seed_schema import HskkTaskType

#: Bumped when the pack's shape changes incompatibly, so import_pack.py can
#: refuse one it does not understand rather than half-read it.
PACK_VERSION: Final = 1


def text_hash(text: str, *, voice: str, speaking_rate: float) -> str:
    """Identity of a piece of synthesised audio.

    Covers the rate as well as the text and voice: the same sentence read at
    0.9 and at 1.0 are different files, and a pack that reused one for the
    other would be wrong in a way only a listener notices.
    """
    payload = json.dumps(
        {"text": text, "voice": voice, "rate": round(speaking_rate, 4)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MediaAsset(BaseModel):
    """One synthesised clip. Mirrors the media_assets row it becomes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The text hash, which is also the file name. Content-addressed so that a
    #: sentence repeated across concepts is synthesised and stored once.
    id: str
    #: Only 'audio' is produced. Video is S2 and PRD 14 forbids generating any
    #: before the content is frozen.
    kind: str = "audio"
    language: str
    #: The logical voice asked for (services/tts/base.py Voice).
    voice: str
    #: What the provider actually used, for media_assets.provider tracing.
    resolved_voice: str = ""
    provider: str
    speaking_rate: float
    #: Kept so a rebuild can tell what a file was made from without the draft.
    text: str
    #: Path inside the pack's media directory, and later the object store key.
    path: str
    duration_ms: int = Field(ge=0)
    bytes: int = Field(ge=0)


class PackSentence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    zh: str
    pinyin: str
    km_gloss: str
    #: MediaAsset.id, or empty when synthesis failed for this one.
    audio: str = ""


class PackSubstitution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    zh: str
    pinyin: str
    km_gloss: str
    audio: str = ""


class PackDialogue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hskk_task_type: HskkTaskType
    prompt_zh: str
    prompt_km: str
    sample_answer_zh: str
    prompt_audio_zh: str = ""
    prompt_audio_km: str = ""


class PackConcept(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str
    standard_ref: str = ""
    pattern: str
    hskk_task_types: tuple[HskkTaskType, ...] = Field(min_length=1)
    km_explanation: str
    explanation_audio: str = ""
    target_sentences: tuple[PackSentence, ...] = Field(min_length=1)
    substitutions: tuple[PackSubstitution, ...] = Field(min_length=1)
    dialogues: tuple[PackDialogue, ...] = Field(min_length=1)


class PackUsage(BaseModel):
    """What the build cost. Becomes a cost_ledger row at import (D-084)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tts_calls: int = 0
    tts_cost_usd_cents: int = 0
    #: Clips that were reused rather than synthesised again.
    reused: int = 0


class Pack(BaseModel):
    """One level, frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_version: int = PACK_VERSION
    #: The content pack version that goes into content_packs.version.
    version: str
    language: str
    level: str
    licence: str = Field(min_length=1)
    sources: tuple[dict[str, Any], ...] = Field(min_length=1)
    built_at: dt.datetime
    provider: str
    #: False when the build was forced past validate.py. import_pack refuses
    #: those: the override has to leave a trace that survives the shell it was
    #: typed in (D-094).
    validated: bool
    #: PRD 14 forbids generating video before the content is frozen, so this is
    #: false in every pack built before S2. The field exists so that a pack
    #: which does carry video is distinguishable.
    with_video: bool = False
    usage: PackUsage
    concepts: tuple[PackConcept, ...] = Field(min_length=1)
    media: tuple[MediaAsset, ...] = ()
    #: sha256 over everything above. Empty until sealed.
    checksum: str = ""

    def sealed(self) -> Pack:
        """A copy carrying the checksum of its own contents."""
        body = self.model_dump(mode="json", exclude={"checksum"})
        digest = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return self.model_copy(update={"checksum": digest})

    @property
    def checksum_matches(self) -> bool:
        return bool(self.checksum) and self.sealed().checksum == self.checksum

    @property
    def missing_audio(self) -> tuple[str, ...]:
        """Items whose audio did not synthesise.

        A pack can still be built with these — ARCHITECTURE section 5 says a
        TTS failure marks the item and the pack is still usable, degrading to
        text at runtime — but the caller has to be told how many.
        """
        gaps: list[str] = []
        for concept in self.concepts:
            if not concept.explanation_audio:
                gaps.append(f"{concept.slug}/km_explanation")
            for index, sentence in enumerate(concept.target_sentences):
                if not sentence.audio:
                    gaps.append(f"{concept.slug}/target_sentences[{index}]")
            for index, substitution in enumerate(concept.substitutions):
                if not substitution.audio:
                    gaps.append(f"{concept.slug}/substitutions[{index}]")
            for index, dialogue in enumerate(concept.dialogues):
                if not dialogue.prompt_audio_zh:
                    gaps.append(f"{concept.slug}/dialogues[{index}].prompt_zh")
                if not dialogue.prompt_audio_km:
                    gaps.append(f"{concept.slug}/dialogues[{index}].prompt_km")
        return tuple(gaps)

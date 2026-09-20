"""Stage [5]: freeze a draft into a pack, with its audio (BACKLOG E6, PRD 6.3).

Every piece of fixed text a learner will hear is synthesised here, once, and
written to a file named after the hash of the text that produced it. At runtime
nothing calls a TTS provider — the client is handed a URL (services/tts/base.py
says so in as many words, and red line R1 is the same rule for the LLM).

    make build                       # FakeTTS: free, deterministic, silent mp3
    make build DRAFT=... OUT=...

Three refusals are built in.

**A draft that has not passed validate.py is not built** unless ``--allow-invalid``
is given, and a pack built that way records ``validated: false`` so the override
survives the shell it was typed in (D-094).

**A billed TTS provider needs ``--confirm-spend``**, the same guard generate.py
has. A level is a thousand-odd clips.

**``--with-video`` refuses**. The switch exists because BACKLOG E6 asks for it
and because a pack has to be able to say whether it carries video, but PRD 14
forbids generating any before the content is frozen: at M1 the sentences are
still changing, and video costs $1-4 a minute to regenerate where audio costs
nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from app.core.config import Settings, get_settings
from app.services.tts.base import SynthesisResult, TTSProvider, Voice
from app.services.tts.registry import get_tts, speaking_rate_for
from pipeline.draft import Draft
from pipeline.pack import (
    MediaAsset,
    Pack,
    PackConcept,
    PackDialogue,
    PackSentence,
    PackSubstitution,
    PackUsage,
    text_hash,
)
from pipeline.validate import validate
from pipeline.wordlist import MissingWordlistError, load_wordlist

_ROOT: Final = Path(__file__).resolve().parents[1]

PACKS_DIR: Final = _ROOT / "pipeline" / "packs"

#: Which logical voice reads which language (services/tts/base.py Voice).
#: A table rather than a branch, because adding English in v2 is a row.
VOICE_FOR: Final[dict[str, Voice]] = {
    "zh": Voice.ZH_MODEL,
    "km": Voice.KM_NARRATOR,
}


class Synthesiser:
    """Turns text into media assets, once per distinct text.

    Content-addressed: the same sentence in two concepts is one file and one
    billed call. On a real provider that is most of the saving — a level
    repeats its substitutions heavily — and it is also what makes a rebuild
    after a small edit cheap.
    """

    def __init__(self, *, settings: Settings, media_dir: Path) -> None:
        self._settings = settings
        self._media_dir = media_dir
        self._assets: dict[str, MediaAsset] = {}
        self.calls = 0
        self.reused = 0
        self.cost_usd_cents = 0
        self.failures: list[tuple[str, str]] = []

    @property
    def assets(self) -> tuple[MediaAsset, ...]:
        return tuple(self._assets.values())

    async def say(self, text: str, *, language: str, provider: TTSProvider) -> str:
        """Synthesise ``text`` if it is new, and return the asset id.

        Returns an empty id when the provider failed: ARCHITECTURE section 5
        says a TTS failure marks the item and leaves the pack buildable, with
        the runtime degrading to text, rather than losing the whole level.
        """
        if not text.strip():
            return ""
        voice = VOICE_FOR[language]
        rate = speaking_rate_for(voice, settings=self._settings)
        identity = text_hash(text, voice=voice.value, speaking_rate=rate)

        if identity in self._assets:
            self.reused += 1
            return identity

        result: SynthesisResult = await provider.synthesize(text, voice=voice, speaking_rate=rate)
        self.calls += 1
        self.cost_usd_cents += result.cost_usd_cents
        if not result.ok:
            self.failures.append((text, result.error_code or "tts.failed"))
            return ""

        path = f"{language}/{identity[:2]}/{identity}.{result.audio_format}"
        destination = self._media_dir / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(result.audio)

        self._assets[identity] = MediaAsset(
            id=identity,
            language=language,
            voice=voice.value,
            resolved_voice=result.resolved_voice,
            provider=provider.name,
            speaking_rate=rate,
            text=text,
            path=path,
            duration_ms=result.duration_ms,
            bytes=len(result.audio),
        )
        return identity


async def build(
    draft: Draft,
    *,
    settings: Settings,
    media_dir: Path,
    version: str,
    validated: bool,
    now: dt.datetime | None = None,
) -> Pack:
    """Freeze a draft, synthesising every fixed string it contains."""
    zh = get_tts(Voice.ZH_MODEL, settings=settings)
    km = get_tts(Voice.KM_NARRATOR, settings=settings)
    synth = Synthesiser(settings=settings, media_dir=media_dir)

    concepts: list[PackConcept] = []
    for concept in draft.concepts:
        sentences = [
            PackSentence(
                zh=sentence.zh,
                pinyin=sentence.pinyin,
                km_gloss=sentence.km_gloss,
                audio=await synth.say(sentence.zh, language="zh", provider=zh),
            )
            for sentence in concept.target_sentences
        ]
        substitutions = [
            PackSubstitution(
                zh=substitution.zh,
                pinyin=substitution.pinyin,
                km_gloss=substitution.km_gloss,
                audio=await synth.say(substitution.zh, language="zh", provider=zh),
            )
            for substitution in concept.substitutions
        ]
        dialogues = [
            PackDialogue(
                hskk_task_type=dialogue.hskk_task_type,
                prompt_zh=dialogue.prompt_zh,
                prompt_km=dialogue.prompt_km,
                sample_answer_zh=dialogue.sample_answer_zh,
                prompt_audio_zh=await synth.say(dialogue.prompt_zh, language="zh", provider=zh),
                prompt_audio_km=await synth.say(dialogue.prompt_km, language="km", provider=km),
            )
            for dialogue in concept.dialogues
        ]
        concepts.append(
            PackConcept(
                slug=concept.slug,
                standard_ref=concept.standard_ref,
                pattern=concept.pattern,
                hskk_task_types=concept.hskk_task_types,
                km_explanation=concept.km_explanation,
                explanation_audio=await synth.say(
                    concept.km_explanation, language="km", provider=km
                ),
                target_sentences=tuple(sentences),
                substitutions=tuple(substitutions),
                dialogues=tuple(dialogues),
            )
        )

    return Pack(
        version=version,
        language=draft.language,
        level=draft.level,
        licence=draft.licence,
        sources=draft.sources,
        built_at=now or dt.datetime.now(dt.UTC),
        provider=zh.name,
        validated=validated,
        with_video=False,
        usage=PackUsage(
            tts_calls=synth.calls,
            tts_cost_usd_cents=synth.cost_usd_cents,
            reused=synth.reused,
        ),
        concepts=tuple(concepts),
        media=synth.assets,
    ).sealed()


def write_pack(pack: Pack, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pack.model_dump_json(indent=2, exclude_none=False) + "\n", encoding="utf-8")


def _validation_problems(draft: Draft, settings: Settings) -> list[str] | None:
    """Violations from validate.py, or None when the rules could not be run."""
    try:
        wordlist = load_wordlist(draft.language, draft.level)
    except (MissingWordlistError, ValueError):
        return None
    return [str(problem) for problem in validate(draft, settings=settings, wordlist=wordlist)]


def _summarise(problems: Iterable[str], limit: int = 5) -> list[str]:
    listed = list(problems)
    shown = listed[:limit]
    if len(listed) > limit:
        shown.append(f"... and {len(listed) - limit} more")
    return shown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("draft", type=Path, help="the draft JSON written by generate.py")
    parser.add_argument("--out", type=Path, default=None, help="where to write the pack")
    parser.add_argument(
        "--media-dir", type=Path, default=None, help="where to write the audio files"
    )
    parser.add_argument("--version", default="v1", help="content pack version string")
    parser.add_argument(
        "--allow-invalid",
        action="store_true",
        help="build anyway when validate.py reports violations; the pack records it",
    )
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="required when the configured TTS provider is not 'fake'",
    )
    parser.add_argument(
        "--with-video",
        action="store_true",
        help="refused before S2: PRD 14 forbids generating video while content is unfrozen",
    )
    arguments = parser.parse_args(argv)

    settings = get_settings()

    if arguments.with_video or settings.pipeline_with_video:
        print(
            "error: video generation is not implemented. PRD section 14 makes it a "
            "non-goal for M1 and M2: the content is still changing, and video costs "
            "$1-4 a minute to regenerate where audio costs nothing. The entry "
            "conditions are in PRD 7.2 (S2).",
            file=sys.stderr,
        )
        return 2

    if settings.tts_provider != "fake" and not arguments.confirm_spend:
        print(
            f"error: TTS_PROVIDER={settings.tts_provider!r} bills per character, and a "
            "level is upwards of a thousand clips. Re-run with --confirm-spend once "
            "the budget is agreed (PRD 11.3 records it as content_production).",
            file=sys.stderr,
        )
        return 2

    try:
        draft = Draft.model_validate_json(arguments.draft.read_text(encoding="utf-8"))
    except OSError as error:
        print(f"error: cannot read {arguments.draft}: {error.strerror or error}", file=sys.stderr)
        return 1

    problems = _validation_problems(draft, settings)
    if problems is None:
        print(
            f"error: no word list for {draft.language} {draft.level}, so validate.py "
            "cannot judge this draft. Build refuses rather than freezing content "
            "nothing checked.",
            file=sys.stderr,
        )
        return 2
    if problems and not arguments.allow_invalid:
        for line in _summarise(problems):
            print(f"error: {line}", file=sys.stderr)
        print(
            f"error: {len(problems)} violation(s); fix them or pass --allow-invalid, "
            "which marks the pack unvalidated and import_pack will refuse it.",
            file=sys.stderr,
        )
        return 1

    out = arguments.out or PACKS_DIR / f"{draft.language}-{draft.level}.pack.json"
    media_dir = arguments.media_dir or out.parent / f"{draft.language}-{draft.level}-media"

    pack = asyncio.run(
        build(
            draft,
            settings=settings,
            media_dir=media_dir,
            version=arguments.version,
            validated=not problems,
        )
    )
    write_pack(pack, out)

    print(
        f"{out}: {len(pack.concepts)} concept(s), {len(pack.media)} clip(s) in {media_dir} "
        f"({pack.usage.tts_calls} call(s), {pack.usage.reused} reused, "
        f"{pack.usage.tts_cost_usd_cents} usd cents)"
    )
    if not pack.validated:
        print("warning: built from an unvalidated draft; import_pack will refuse it.")
    gaps = pack.missing_audio
    if gaps:
        print(f"warning: {len(gaps)} item(s) have no audio and will degrade to text:")
        for line in _summarise(gaps):
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

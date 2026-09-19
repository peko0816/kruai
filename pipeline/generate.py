"""Stage [2]: write the content for each seeded concept with a language model.

BACKLOG E3, PRD 6.1. Offline and batched — the model is asked once per concept,
through ``llm.batch_complete``, and nothing here runs while a learner is
waiting. That separation is red line R1: course content is data, produced ahead
of time and frozen, never generated inside a request.

What comes back is a *draft*. validate.py judges it (E4), a native speaker
samples it (E5), build_pack.py freezes it (E6). This stage is only responsible
for asking well and for refusing to pretend a failed call succeeded.

Run it::

    make generate                      # the HSK1 seed, provider from .env
    python -m pipeline.generate --limit 3      # a three-concept smoke run

With ``LLM_PROVIDER=fake`` this costs nothing and produces structurally valid
placeholder content, which is what the rest of the pipeline is built against.
Pointing it at a real provider spends real money, so it refuses to start
without ``--confirm-spend`` (D-085).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.services.llm.base import CompletionResult, LLMProvider, Message, Purpose
from app.services.llm.registry import get_llm
from pipeline.draft import ConceptDraft, Draft, Failure, Usage
from pipeline.seed_schema import SeedConcept, SeedError, SeedFile, load_seed_file

_ROOT: Final = Path(__file__).resolve().parents[1]

#: Where drafts land. .gitignore excludes pipeline/packs/*.json: they are large
#: and regenerable, and a committed draft would start being edited by hand.
PACKS_DIR: Final = _ROOT / "pipeline" / "packs"

DEFAULT_SEED: Final = _ROOT / "pipeline" / "seed" / "zh-hsk3.0" / "hsk1.yaml"


# ---------------------------------------------------------------------------
# Prompt construction
#
#                        *** RED LINE R7 — READ THIS ***
#
# PRD section 5.5 forbids publisher textbooks from entering the pipeline, and
# says in as many words that the ban covers **using them as few-shot examples**,
# not only copying from them. That is the rule this comment exists for, because
# this function is the one place where it would be broken without anybody
# noticing: a prompt is not reviewed like content is, and "here are ten example
# sentences from a textbook, write more like these" leaves no trace in the
# output that review would catch.
#
# So the prompt carries exactly two kinds of material:
#
#   1. the concept's own seed fields — pattern, syllabus reference, and the
#      example sentences the seed cites from a whitelisted source
#      (pipeline/seed_sources.py, enforced at E1);
#   2. instructions written here, in this file, about form and level.
#
# **Nothing else may be added.** Not a sample lesson, not "in the style of", not
# a few-shot block pasted from anywhere. If a future change needs examples the
# seed does not have, they go into the seed first, where their source is
# declared and checked.
# ---------------------------------------------------------------------------

#: Byte-stable across every call in a run, because base.py's contract promises
#: prompt caching on a verbatim-identical system prompt and PRD 4.2 asks for it.
#: Interpolating anything per concept here would cost a cache hit on every call.
SYSTEM_PROMPT: Final = (
    "You write material for a Chinese speaking course whose learners are "
    "Khmer speakers in Cambodia. You are given one grammar point from the "
    "Chinese national proficiency standard, with its pattern and a few example "
    "sentences taken from that standard.\n"
    "\n"
    "Rules:\n"
    "1. Every Chinese sentence must use only vocabulary and characters at or "
    "below the stated level, and must be natural spoken Chinese a beginner "
    "would actually say.\n"
    "2. Pinyin uses tone diacritics (nǐ hǎo), never tone digits, and must "
    "match the characters exactly.\n"
    "3. Khmer text must be written in Khmer script. Never romanised Khmer, "
    "never English in a Khmer field.\n"
    "4. Sentences must differ from one another in more than one word.\n"
    "5. Reproduce nothing from any textbook. Write original sentences for the "
    "pattern you are given.\n"
    "\n"
    "Answer with JSON matching the schema you are given, and nothing else."
)


def response_schema(
    *, min_sentences: int, min_substitutions: int, min_dialogues: int, task_types: tuple[str, ...]
) -> dict[str, Any]:
    """The JSON schema one concept's answer must satisfy.

    The minimums come from configuration (PIPELINE_MIN_*), not from this file:
    they are the same numbers validate.py enforces, and two copies of a
    threshold is one copy too many (red line R3). They travel to the provider
    as ``minItems``, which is also what makes FakeLLM emit a draft of the right
    size to exercise the stages downstream.
    """
    string = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["km_explanation", "target_sentences", "substitutions", "dialogues"],
        "properties": {
            "km_explanation": {
                **string,
                "description": "How this pattern works, in Khmer script, for a beginner.",
            },
            "target_sentences": {
                "type": "array",
                "minItems": min_sentences,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["zh", "pinyin", "km_gloss"],
                    "properties": {
                        "zh": {**string, "description": "The sentence, in Chinese characters."},
                        "pinyin": {**string, "description": "Tone diacritics, e.g. wǒ yào shuǐ."},
                        "km_gloss": {**string, "description": "What it means, in Khmer script."},
                    },
                },
            },
            "substitutions": {
                "type": "array",
                "minItems": min_substitutions,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["zh", "pinyin", "km_gloss"],
                    "properties": {
                        "zh": {**string, "description": "One filler for the slot in the pattern."},
                        "pinyin": string,
                        "km_gloss": string,
                    },
                },
            },
            "dialogues": {
                "type": "array",
                "minItems": min_dialogues,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "hskk_task_type",
                        "prompt_zh",
                        "prompt_km",
                        "sample_answer_zh",
                    ],
                    "properties": {
                        # Constrained to the task types this concept declares, so
                        # the model cannot invent a fourth kind of turn.
                        "hskk_task_type": {"type": "string", "enum": list(task_types)},
                        "prompt_zh": {**string, "description": "What the learner is asked."},
                        "prompt_km": {**string, "description": "The same, in Khmer script."},
                        "sample_answer_zh": {**string, "description": "One acceptable answer."},
                    },
                },
            },
        },
    }


def concept_messages(concept: SeedConcept, *, seed: SeedFile) -> list[Message]:
    """The prompt for one concept. See the R7 block above before editing."""
    lines = [
        f"Level: {seed.meta.level} ({seed.meta.language}).",
        f"Grammar point: {concept.standard_ref or '(unnumbered)'}",
        f"Pattern: {concept.pattern}",
        f"Spoken task types this point is graded as: {', '.join(concept.hskk_task_types)}.",
    ]
    if concept.example_sentences:
        lines.append(
            "Example sentences from the standard, for calibration of level and "
            "form (do not simply repeat them): " + " ".join(concept.example_sentences)
        )
    return [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content="\n".join(lines)),
    ]


# ---------------------------------------------------------------------------
# Running


def _usage_of(results: list[CompletionResult]) -> Usage:
    return Usage(
        calls=len(results),
        input_tokens=sum(r.input_tokens for r in results),
        output_tokens=sum(r.output_tokens for r in results),
        cached_input_tokens=sum(r.cached_input_tokens for r in results),
        cost_usd_cents=sum(r.cost_usd_cents for r in results),
    )


def _merge(concept: SeedConcept, parsed: dict[str, Any]) -> ConceptDraft:
    """Seed fields plus generated fields. Raises ValidationError if they do not fit."""
    return ConceptDraft.model_validate(
        {
            "slug": concept.slug,
            "standard_ref": concept.standard_ref,
            "pattern": concept.pattern,
            "hskk_task_types": list(concept.hskk_task_types),
            **parsed,
        }
    )


async def generate(
    seed: SeedFile,
    *,
    llm: LLMProvider,
    settings: Settings,
    seed_file: Path,
    limit: int | None = None,
    now: dt.datetime | None = None,
) -> Draft:
    """Ask the provider for every concept in ``seed`` and assemble the draft.

    One request per concept, not one for the whole level. A level is 48
    concepts and several thousand tokens of output; a single response that has
    to be right about all of it fails all-or-nothing, and a partial failure is
    unattributable. Per concept, a bad answer costs one concept, and PRD 6.1
    sends exactly those round again.

    Concepts are batched in groups that share a set of HSKK task types, because
    ``batch_complete`` takes one schema for the whole batch and the schema
    pins ``hskk_task_type`` to the types that concept actually declares. Three
    groups cover HSK1. The alternative — one permissive schema and a check
    afterwards — pays for content that then gets thrown away.
    """
    concepts = list(seed.concepts[:limit] if limit is not None else seed.concepts)

    groups: dict[tuple[str, ...], list[SeedConcept]] = {}
    for concept in concepts:
        groups.setdefault(tuple(concept.hskk_task_types), []).append(concept)

    drafted: dict[str, ConceptDraft] = {}
    failures: dict[str, Failure] = {}
    every_result: list[CompletionResult] = []
    model = ""

    for task_types, members in groups.items():
        results = await llm.batch_complete(
            [concept_messages(concept, seed=seed) for concept in members],
            purpose=Purpose.OFFLINE_GENERATION,
            json_schema=response_schema(
                min_sentences=settings.pipeline_min_sentences_per_concept,
                min_substitutions=settings.pipeline_min_substitutions_per_concept,
                min_dialogues=settings.pipeline_min_dialogues_per_concept,
                task_types=task_types,
            ),
        )
        every_result.extend(results)

        # The contract says the order matches and failures come back as
        # placeholders rather than being dropped. Trusting that silently would
        # misfile every concept after a provider that got it wrong, so it is
        # checked rather than assumed.
        if len(results) != len(members):
            raise RuntimeError(
                f"{llm.name} returned {len(results)} results for {len(members)} requests; "
                "batch_complete must answer one-to-one (services/llm/base.py)"
            )

        for concept, result in zip(members, results, strict=True):
            model = model or result.model
            if not result.ok or result.parsed is None:
                failures[concept.slug] = Failure(
                    slug=concept.slug,
                    error_code=result.error_code or "llm.no_parsed_output",
                    error_message=result.error_message or "provider returned no parsed object",
                )
                continue
            try:
                drafted[concept.slug] = _merge(concept, result.parsed)
            except ValidationError as error:
                # The provider said ok, and what came back does not fit. That is
                # a failure of this concept, not a reason to write a half-draft.
                failures[concept.slug] = Failure(
                    slug=concept.slug,
                    error_code="draft.schema_mismatch",
                    error_message="; ".join(
                        f"{'.'.join(str(p) for p in detail['loc'])}: {detail['msg']}"
                        for detail in error.errors()[:5]
                    ),
                )

    # Seed order, not group order: the draft reads in teaching order.
    return Draft(
        language=seed.meta.language,
        level=seed.meta.level,
        licence=seed.meta.licence,
        sources=tuple(source.model_dump() for source in seed.meta.sources),
        seed_file=str(seed_file),
        provider=llm.name,
        model=model,
        generated_at=now or dt.datetime.now(dt.UTC),
        usage=_usage_of(every_result),
        concepts=tuple(drafted[c.slug] for c in concepts if c.slug in drafted),
        failures=tuple(failures[c.slug] for c in concepts if c.slug in failures),
    )


def draft_path(seed: SeedFile, *, directory: Path = PACKS_DIR) -> Path:
    return directory / f"{seed.meta.language}-{seed.meta.level}.draft.json"


def write_draft(draft: Draft, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(draft.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--seed", type=Path, default=DEFAULT_SEED, help="seed file to generate from"
    )
    parser.add_argument("--out", type=Path, default=None, help="where to write the draft")
    parser.add_argument(
        "--limit", type=int, default=None, help="only the first N concepts (a smoke run)"
    )
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="required when the configured provider is not 'fake'; generation is billed per token",
    )
    arguments = parser.parse_args(argv)

    settings = get_settings()

    # CLAUDE.md section 6: spending money is one of the four things that stop
    # and ask. A full level is 48 billed calls, and the difference between a
    # free run and a paid one is one line in .env that nobody re-reads before
    # typing `make generate` (D-085).
    if settings.llm_provider != "fake" and not arguments.confirm_spend:
        print(
            f"error: LLM_PROVIDER={settings.llm_provider!r} bills per token. "
            "Re-run with --confirm-spend once you have agreed the budget "
            "(PRD 11.3 records it as content_production).",
            file=sys.stderr,
        )
        return 2

    try:
        seed = load_seed_file(arguments.seed)
    except SeedError as error:
        for problem in error.problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    llm = get_llm(settings=settings)
    draft = asyncio.run(
        generate(seed, llm=llm, settings=settings, seed_file=arguments.seed, limit=arguments.limit)
    )

    out = arguments.out or draft_path(seed)
    write_draft(draft, out)

    print(
        f"{out}: {len(draft.concepts)} concept(s) drafted by {draft.provider} "
        f"({draft.usage.calls} call(s), {draft.usage.cost_usd_cents} usd cents, "
        f"{draft.usage.cached_input_tokens} cached input tokens)"
    )
    for failure in draft.failures:
        print(
            f"error: {failure.slug}: {failure.error_code}: {failure.error_message}", file=sys.stderr
        )
    if draft.failures:
        print(
            f"error: {len(draft.failures)} concept(s) did not generate; "
            "the draft holds the rest, re-run to fill them in (PRD 6.1)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

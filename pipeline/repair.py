"""Stage [4b]. Ask again for the concepts that failed, saying what was wrong.

generate.py asks once. validate.py judges. Between them there was nothing: a
draft with violations could only be regenerated whole, which pays for the 40
concepts that were already fine to get at the 8 that were not, and throws away
the ones that happened to come out well.

This is that missing step. It re-asks **only** the concepts validate.py
reported, with their own violations quoted into the prompt, and keeps whichever
version of each concept has fewer violations. A repair pass that can make a
concept worse is not a repair pass, so the comparison is the point rather than
a refinement of it: the model is being asked to fix eight things at once and
may well break a ninth.

R1 is untouched — this runs offline over a seed, like generate.py, and nothing
here is reachable from a request.

    make repair                      # over pipeline/packs/<pack>.draft.json
    make repair ROUNDS=2
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Final

from app.core.config import Settings, get_settings
from app.services.llm.base import CompletionResult, LLMProvider, Message, Purpose
from app.services.llm.registry import get_llm
from pipeline.draft import ConceptDraft, Draft
from pipeline.generate import (
    DEFAULT_SEED,
    concept_messages,
    draft_path,
    response_schema,
    write_draft,
)
from pipeline.seed_schema import SeedConcept, SeedError, SeedFile, load_seed_file
from pipeline.validate import Violation, max_characters, validate
from pipeline.wordlist import MissingWordlistError, Wordlist, load_wordlist

#: ``concepts[7](zh.hsk1.want_noun).target_sentences[3]`` — the slug is what
#: identifies a concept across two drafts, since the index may move.
_SLUG: Final = re.compile(r"concepts\[\d+\]\(([^)]+)\)")


def by_slug(problems: list[Violation]) -> dict[str, list[Violation]]:
    """Violations grouped by the concept they belong to.

    Draft-level ones (sources, licence) have no slug and are dropped here on
    purpose: re-asking a concept cannot fix the draft's provenance.
    """
    grouped: dict[str, list[Violation]] = defaultdict(list)
    for problem in problems:
        found = _SLUG.search(problem.where)
        if found:
            grouped[found.group(1)].append(problem)
    return dict(grouped)


def repair_messages(
    concept: SeedConcept,
    *,
    seed: SeedFile,
    problems: list[Violation],
    wordlist: Wordlist | None,
    max_chars: int,
    **counts: int,
) -> list[Message]:
    """The original prompt, plus what came back wrong last time.

    The violations are quoted rather than summarised. "Outside the HSK1
    vocabulary: ['苹果']" tells the model which word to replace; "some words
    were too advanced" tells it to try again and hope.
    """
    messages = concept_messages(
        concept, seed=seed, wordlist=wordlist, max_chars=max_chars, **counts
    )
    listed = "\n".join(f"- {problem.rule}: {problem.message}" for problem in problems)
    return [
        *messages[:-1],
        Message(
            role="user",
            content=(
                f"{messages[-1].content}\n\n"
                "A previous answer to this exact request was rejected for the "
                "reasons below. Write a new answer that does not repeat any of "
                "them. Everything else about the request is unchanged.\n"
                f"{listed}"
            ),
        ),
    ]


def _counts(settings: Settings) -> dict[str, int]:
    return {
        "min_sentences": settings.pipeline_min_sentences_per_concept,
        "min_substitutions": settings.pipeline_min_substitutions_per_concept,
        "min_dialogues": settings.pipeline_min_dialogues_per_concept,
    }


def _replace(draft: Draft, replacements: dict[str, ConceptDraft]) -> Draft:
    return draft.model_copy(
        update={
            "concepts": tuple(replacements.get(concept.slug, concept) for concept in draft.concepts)
        }
    )


async def repair_once(
    draft: Draft,
    seed: SeedFile,
    *,
    llm: LLMProvider,
    settings: Settings,
    wordlist: Wordlist,
) -> tuple[Draft, list[CompletionResult], dict[str, tuple[int, int]]]:
    """One round. Returns the draft, what it cost, and slug -> (before, after)."""
    # No early return for a clean draft: with nothing to re-ask, the loop below
    # makes no calls and the `not answers` branch returns the same thing. A
    # guard that cannot change the answer only reads like one that can.
    before = by_slug(validate(draft, settings=settings, wordlist=wordlist))
    seeded = {concept.slug: concept for concept in seed.concepts}
    wanted = [slug for slug in before if slug in seeded]
    counts = _counts(settings)
    schema_for = {
        slug: response_schema(task_types=tuple(seeded[slug].hskk_task_types), **counts)
        for slug in wanted
    }

    #: One schema per batch, so concepts are grouped by their task types the
    #: same way generate.py groups them.
    grouped: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for slug in wanted:
        grouped[tuple(seeded[slug].hskk_task_types)].append(slug)

    answers: dict[str, ConceptDraft] = {}
    spent: list[CompletionResult] = []
    for slugs in grouped.values():
        results = await llm.batch_complete(
            [
                repair_messages(
                    seeded[slug],
                    seed=seed,
                    problems=before[slug],
                    wordlist=wordlist,
                    max_chars=max_characters(draft.level, settings),
                    **counts,
                )
                for slug in slugs
            ],
            purpose=Purpose.OFFLINE_GENERATION,
            json_schema=schema_for[slugs[0]],
            max_output_tokens=settings.pipeline_max_output_tokens,
        )
        spent.extend(results)
        for slug, result in zip(slugs, results, strict=True):
            if result.parsed is None:
                continue
            concept = seeded[slug]
            try:
                answers[slug] = ConceptDraft.model_validate(
                    {
                        "slug": concept.slug,
                        "standard_ref": concept.standard_ref,
                        "pattern": concept.pattern,
                        "hskk_task_types": list(concept.hskk_task_types),
                        **result.parsed,
                    }
                )
            except Exception:  # a malformed answer is simply not an improvement
                continue

    if not answers:
        return draft, spent, {}

    # Judge the candidates in place, against the rest of the draft: duplicate
    # detection is cross-concept, so a sentence is only a duplicate in company.
    candidate = _replace(draft, answers)
    after = by_slug(validate(candidate, settings=settings, wordlist=wordlist))

    kept: dict[str, ConceptDraft] = {}
    moved: dict[str, tuple[int, int]] = {}
    for slug, answer in answers.items():
        was, now = len(before.get(slug, [])), len(after.get(slug, []))
        moved[slug] = (was, now)
        if now < was:
            kept[slug] = answer

    return _replace(draft, kept), spent, moved


async def repair(
    draft: Draft,
    seed: SeedFile,
    *,
    llm: LLMProvider,
    settings: Settings,
    wordlist: Wordlist,
    rounds: int = 1,
) -> tuple[Draft, list[CompletionResult]]:
    spent: list[CompletionResult] = []
    for number in range(1, rounds + 1):
        draft, results, moved = await repair_once(
            draft, seed, llm=llm, settings=settings, wordlist=wordlist
        )
        spent.extend(results)
        improved = sum(1 for was, now in moved.values() if now < was)
        print(
            f"round {number}: re-asked {len(moved)} concept(s), kept {improved}",
            file=sys.stderr,
        )
        if not moved or not improved:
            break
    return draft, spent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--draft", type=Path, default=None)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="required when the configured provider is not 'fake'; repair is billed per token",
    )
    arguments = parser.parse_args(argv)

    settings = get_settings()
    if settings.llm_provider != "fake" and not arguments.confirm_spend:
        print(
            f"error: LLM_PROVIDER={settings.llm_provider!r} bills per token. "
            "Re-run with --confirm-spend once you have agreed the budget.",
            file=sys.stderr,
        )
        return 2

    try:
        seed = load_seed_file(arguments.seed)
    except SeedError as error:
        for problem in error.problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    path = arguments.draft or draft_path(seed)
    try:
        draft = Draft.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as error:
        print(f"error: cannot read {path}: {error.strerror or error}", file=sys.stderr)
        return 1

    try:
        wordlist = load_wordlist(draft.language, draft.level)
    except (MissingWordlistError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    was = len(validate(draft, settings=settings, wordlist=wordlist))
    repaired, spent = asyncio.run(
        repair(
            draft,
            seed,
            llm=get_llm(settings=settings),
            settings=settings,
            wordlist=wordlist,
            rounds=arguments.rounds,
        )
    )
    now = len(validate(repaired, settings=settings, wordlist=wordlist))

    write_draft(repaired, path)
    print(
        f"{path}: {was} -> {now} violation(s) "
        f"({len(spent)} call(s), {sum(r.cost_usd_cents for r in spent)} usd cents)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

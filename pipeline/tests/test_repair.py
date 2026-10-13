"""The repair pass (pipeline/repair.py).

What earns this stage its place is not that it re-asks — it is that it re-asks
*selectively* and keeps whichever answer is better. A pass that re-asks
everything pays for the concepts that were already fine; a pass that takes the
new answer on faith can hand back a worse draft than it was given, and the
model is being asked to fix eight things at once.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.services.llm.base import CompletionResult, LLMProvider, Message
from pipeline.draft import Draft
from pipeline.repair import by_slug, repair_messages, repair_once
from pipeline.seed_schema import SeedFile, parse_seed
from pipeline.validate import Violation, validate
from pipeline.wordlist import Wordlist

KM = "ខ្ញុំចង់បានទឹក"


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql://unused/unused",
        "redis_url": "redis://unused",
        "telegram_bot_token": "",
        "azure_speech_key": "",
        "azure_speech_region": "",
        "google_application_credentials": "",
        "elevenlabs_api_key": "",
        "openai_api_key": "",
        "payway_merchant_id": "",
        "payway_api_key": "",
        "payway_base_url": "",
        "bakong_token": "",
        "jwt_secret": "x" * 32,
        "pipeline_min_sentences_per_concept": 1,
        "pipeline_min_substitutions_per_concept": 1,
        "pipeline_min_dialogues_per_concept": 1,
    }
    return Settings(**{**base, **overrides})


def a_wordlist(*words: str) -> Wordlist:
    return Wordlist(language="zh", level="HSK1", words=frozenset(words), sources=("test",))


WORDS = a_wordlist("我", "要", "水", "你", "喝", "茶")


def a_seed() -> SeedFile:
    return parse_seed(
        {
            "schema_version": 1,
            "meta": {
                "language": "zh",
                "level": "HSK1",
                "licence": "public standards",
                "sources": [{"id": "zh.standard.2021"}],
            },
            "concepts": [
                {
                    "slug": "zh.hsk1.want_noun",
                    "standard_ref": "一03",
                    "pattern": "我要 + [名词]",
                    "km_explanation": "[[km:concept.zh.hsk1.want_noun]]",
                    "hskk_task_types": ["listen_and_repeat"],
                    "sort_order": 10,
                }
            ],
        },
        origin=Path("test.yaml"),
    )


def concept(zh: str, pinyin: str = "wǒ yào shuǐ") -> dict[str, Any]:
    return {
        "slug": "zh.hsk1.want_noun",
        "pattern": "我要 + [名词]",
        "hskk_task_types": ["listen_and_repeat"],
        "km_explanation": KM,
        "target_sentences": [{"zh": zh, "pinyin": pinyin, "km_gloss": KM}],
        "substitutions": [{"zh": "水", "pinyin": "shuǐ", "km_gloss": KM}],
        "dialogues": [
            {
                "hskk_task_type": "listen_and_repeat",
                "prompt_zh": "你要水",
                "prompt_km": KM,
                "sample_answer_zh": "我要水",
            }
        ],
    }


def a_draft(zh: str, pinyin: str = "wǒ yào shuǐ") -> Draft:
    return Draft.model_validate(
        {
            "language": "zh",
            "level": "HSK1",
            "licence": "public standards",
            "sources": [{"id": "zh.standard.2021"}],
            "seed_file": "test.yaml",
            "provider": "fake",
            "model": "fake",
            "generated_at": dt.datetime(2026, 9, 19, tzinfo=dt.UTC),
            "usage": {},
            "concepts": [concept(zh, pinyin)],
        }
    )


class Answering(LLMProvider):
    """A provider that replies with whatever sentence it was built with."""

    name = "answering"

    def __init__(self, zh: str, pinyin: str = "wǒ yào shuǐ") -> None:
        self.zh = zh
        self.pinyin = pinyin
        self.prompts: list[str] = []

    async def complete(self, messages: list[Message], **kwargs: Any) -> CompletionResult:
        raise AssertionError("repair goes through batch_complete")

    async def batch_complete(
        self, batches: list[list[Message]], **kwargs: Any
    ) -> list[CompletionResult]:
        for messages in batches:
            self.prompts.append(messages[-1].content)
        return [
            CompletionResult(
                ok=True,
                model="answering",
                parsed=concept(self.zh, self.pinyin) | {"km_explanation": KM},
                input_tokens=1,
                output_tokens=1,
            )
            for _ in batches
        ]


def run(draft: Draft, llm: LLMProvider) -> tuple[Draft, Any, dict[str, tuple[int, int]]]:
    return asyncio.run(repair_once(draft, a_seed(), llm=llm, settings=settings(), wordlist=WORDS))


# --------------------------------------------------------------- what it re-asks


def test_only_the_concepts_that_failed_are_re_asked() -> None:
    clean = a_draft("我要水。")
    assert validate(clean, settings=settings(), wordlist=WORDS) == []

    _, spent, moved = run(clean, Answering("我要水。"))

    assert spent == []
    assert moved == {}


def test_the_violations_themselves_reach_the_prompt() -> None:
    """Quoted, not summarised: "outside the HSK1 vocabulary: ['咖啡']" names the
    word to replace; "too advanced" only says to try again."""
    llm = Answering("我要水。")

    run(a_draft("我要咖啡。", "wǒ yào kā fēi"), llm)

    assert "咖啡" in llm.prompts[0]
    assert "wordlist" in llm.prompts[0]


def test_the_original_request_is_still_in_the_prompt() -> None:
    llm = Answering("我要水。")

    run(a_draft("我要咖啡。", "wǒ yào kā fēi"), llm)

    assert "我要 + [名词]" in llm.prompts[0]


# ------------------------------------------------------- what it keeps and drops


def test_a_better_answer_replaces_the_old_one() -> None:
    repaired, _, moved = run(a_draft("我要咖啡。", "wǒ yào kā fēi"), Answering("我要水。"))

    assert moved == {"zh.hsk1.want_noun": (1, 0)}
    assert repaired.concepts[0].target_sentences[0].zh == "我要水。"
    assert validate(repaired, settings=settings(), wordlist=WORDS) == []


def test_a_worse_answer_is_discarded() -> None:
    """The failure this comparison exists for. Asked to fix one violation, the
    model returns a sentence with two; taking it on faith hands back a draft
    worse than the one it was given."""
    before = a_draft("我要咖啡。", "wǒ yào kā fēi")  # one violation

    repaired, _, moved = run(
        before, Answering("我要咖啡牛奶果汁啤酒。", "wǒ yào kā fēi niú nǎi guǒ zhī pí jiǔ")
    )  # two

    assert moved["zh.hsk1.want_noun"][1] > moved["zh.hsk1.want_noun"][0]
    assert repaired.concepts[0].target_sentences[0].zh == "我要咖啡。"


def test_an_equally_bad_answer_is_discarded_too() -> None:
    """Not an improvement is not an improvement; churning the draft for no
    gain makes the next review diff meaningless."""
    repaired, _, _ = run(
        a_draft("我要咖啡。", "wǒ yào kā fēi"), Answering("我要牛奶。", "wǒ yào niú nǎi")
    )

    assert repaired.concepts[0].target_sentences[0].zh == "我要咖啡。"


def test_an_unparseable_answer_leaves_the_draft_alone() -> None:
    class Broken(Answering):
        async def batch_complete(
            self, batches: list[list[Message]], **kwargs: Any
        ) -> list[CompletionResult]:
            return [
                CompletionResult(
                    ok=False,
                    model="broken",
                    parsed=None,
                    error_code="llm.truncated",
                )
                for _ in batches
            ]

    repaired, _, moved = run(a_draft("我要咖啡。", "wǒ yào kā fēi"), Broken("x"))

    assert moved == {}
    assert repaired.concepts[0].target_sentences[0].zh == "我要咖啡。"


# ------------------------------------------------------------------- the grouping


def test_draft_level_violations_are_not_blamed_on_a_concept() -> None:
    """Re-asking a concept cannot fix the draft's own provenance."""
    grouped = by_slug(
        [
            Violation("sources", "draft", "no sources"),
            Violation("wordlist", "concepts[0](a.b.c).target_sentences[0]", "x"),
        ]
    )

    assert set(grouped) == {"a.b.c"}


def test_the_prompt_builder_keeps_the_system_message() -> None:
    messages = repair_messages(
        a_seed().concepts[0],
        seed=a_seed(),
        problems=[Violation("wordlist", "x", "outside")],
        wordlist=WORDS,
        max_chars=8,
        min_sentences=1,
        min_substitutions=1,
        min_dialogues=1,
    )

    assert [m.role for m in messages] == ["system", "user"]
    assert "水" in messages[0].content

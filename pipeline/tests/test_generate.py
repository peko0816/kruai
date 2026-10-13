"""Generation: what it asks for, what it does with the answer (BACKLOG E3).

E3's acceptance criterion is "structurally valid output under FakeLLM", which
is the first test here. The rest are about the two things that are easy to get
wrong and invisible afterwards: the prompt carrying material it must not
(red line R7), and a failed call being quietly filed as a success.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.llm.base import CompletionResult, LLMProvider, Message
from app.services.llm.fake import FakeLLM
from pipeline import generate as generate_module
from pipeline.draft import ConceptDraft, Draft
from pipeline.generate import (
    concept_messages,
    generate,
    main,
    response_schema,
    system_prompt,
)
from pipeline.seed_schema import SeedFile, load_seed_file, parse_seed
from pipeline.validate_seed import SEED_ROOT

HSK1 = SEED_ROOT / "zh-hsk3.0" / "hsk1.yaml"

#: CJK ideographs. Used to ask "did any Chinese reach the prompt that the seed
#: did not put there?", which is the mechanical half of R7.
_HAN = re.compile(r"[一-鿿]")


def settings(**overrides: Any) -> Settings:
    """Settings without reading .env: these tests are about thresholds, not deployment."""
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
    }
    return Settings(**{**base, **overrides})


def a_seed(**concept_overrides: Any) -> SeedFile:
    document: dict[str, Any] = {
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
                "example_sentences": ["我要水。"],
                "sort_order": 10,
                **concept_overrides,
            }
        ],
    }
    return parse_seed(document, origin=Path("test.yaml"))


async def run(seed: SeedFile, llm: LLMProvider, **kwargs: Any) -> Draft:
    return await generate(
        seed,
        llm=llm,
        settings=kwargs.pop("settings", settings()),
        seed_file=Path("test.yaml"),
        now=dt.datetime(2026, 9, 19, tzinfo=dt.UTC),
        **kwargs,
    )


# ------------------------------------------------------------ the acceptance


async def test_the_whole_hsk1_seed_drafts_under_the_fake_provider() -> None:
    """E3's acceptance criterion, on the real seed rather than a toy one."""
    seed = load_seed_file(HSK1)

    draft = await run(seed, FakeLLM())

    assert draft.failures == ()
    assert len(draft.concepts) == len(seed.concepts) == 48
    assert draft.complete


async def test_the_draft_holds_the_counts_configuration_asks_for() -> None:
    """PIPELINE_MIN_* reaches the provider as minItems, so a draft is usable."""
    configured = settings(
        pipeline_min_sentences_per_concept=8,
        pipeline_min_substitutions_per_concept=12,
        pipeline_min_dialogues_per_concept=3,
    )

    draft = await run(a_seed(), FakeLLM(), settings=configured)
    concept = draft.concepts[0]

    assert len(concept.target_sentences) == 8
    assert len(concept.substitutions) == 12
    assert len(concept.dialogues) == 3


async def test_changing_the_configured_minimum_changes_what_is_asked_for() -> None:
    """The thresholds are read, not hardcoded beside a copy of themselves (R3)."""
    draft = await run(a_seed(), FakeLLM(), settings=settings(pipeline_min_sentences_per_concept=2))

    assert len(draft.concepts[0].target_sentences) == 2


def test_the_schema_pins_the_task_types_to_what_the_concept_declares() -> None:
    schema = response_schema(
        min_sentences=1,
        min_substitutions=1,
        min_dialogues=1,
        task_types=("listen_and_repeat", "answer_questions"),
    )

    enum = schema["properties"]["dialogues"]["items"]["properties"]["hskk_task_type"]["enum"]

    assert enum == ["listen_and_repeat", "answer_questions"]


async def test_a_concept_only_gets_dialogue_types_it_is_graded_as() -> None:
    seed = load_seed_file(HSK1)

    draft = await run(seed, FakeLLM())

    for concept in draft.concepts:
        for dialogue in concept.dialogues:
            assert dialogue.hskk_task_type in concept.hskk_task_types


async def test_the_draft_reads_in_teaching_order_not_in_batch_order() -> None:
    """Concepts are batched by task type; the file must still read in order."""
    seed = load_seed_file(HSK1)

    draft = await run(seed, FakeLLM())

    assert [c.slug for c in draft.concepts] == [c.slug for c in seed.concepts]


async def test_provenance_travels_with_the_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """R7: a draft that lost its sources could not be imported (PRD 5.5)."""
    seed = a_seed()

    draft = await run(seed, FakeLLM())

    assert draft.licence == seed.meta.licence
    assert [source["id"] for source in draft.sources] == ["zh.standard.2021"]
    assert draft.language == "zh"
    assert draft.level == "HSK1"


# ------------------------------------------------------------------ red line R7


async def test_the_prompt_carries_no_chinese_the_seed_did_not_supply() -> None:
    """The mechanical half of R7 (PRD 5.5), which the prompt is where it breaks.

    A textbook used as a few-shot example leaves no trace in the output that a
    reviewer could catch — but it cannot reach the model without putting
    Chinese into the prompt that the seed does not contain. So that is what is
    asserted, against the real 48-concept seed.
    """
    seed = load_seed_file(HSK1)

    for concept in seed.concepts:
        # The syllabus reference is 一NN, and 一 is a CJK ideograph.
        allowed = set(_HAN.findall(concept.pattern + concept.standard_ref))
        for sentence in concept.example_sentences:
            allowed |= set(_HAN.findall(sentence))

        for message in concept_messages(concept, seed=seed):
            unexpected = set(_HAN.findall(message.content)) - allowed
            assert unexpected == set(), f"{concept.slug}: prompt introduced {sorted(unexpected)}"


def test_the_system_prompt_names_the_textbook_rule() -> None:
    """Belt and braces: the model is told, as well as not being shown."""
    assert "textbook" in generate_module.SYSTEM_PROMPT.lower()


def test_the_system_prompt_is_byte_stable_across_concepts() -> None:
    """base.py promises prompt caching on a verbatim-identical system prompt."""
    seed = load_seed_file(HSK1)

    systems = {
        message.content
        for concept in seed.concepts
        for message in concept_messages(concept, seed=seed)
        if message.role == "system"
    }

    assert len(systems) == 1


async def test_the_run_reports_the_cache_hits_it_got() -> None:
    """If this goes to zero, every call is paying full input price (PRD 11.3)."""
    seed = load_seed_file(HSK1)

    draft = await run(seed, FakeLLM())

    assert draft.usage.calls == 48
    assert draft.usage.cached_input_tokens > 0


# ------------------------------------------------------------------- failures


class _Failing(LLMProvider):
    """Fails the very first request of the run, the way base.py says failures arrive.

    Stateful across batches on purpose: concepts are grouped by task type, so
    "the first of each batch" would be three failures and would hide whether
    the right concept was blamed.
    """

    name = "failing"

    def __init__(self) -> None:
        self._seen = 0

    async def complete(self, messages: list[Message], **kwargs: Any) -> CompletionResult:
        raise NotImplementedError

    async def batch_complete(
        self, batches: list[list[Message]], **kwargs: Any
    ) -> list[CompletionResult]:
        fake = FakeLLM()
        results = await fake.batch_complete(batches, **kwargs)
        out: list[CompletionResult] = []
        for result in results:
            self._seen += 1
            out.append(
                CompletionResult(ok=False, error_code="llm.timeout", error_message="took too long")
                if self._seen == 1
                else result
            )
        return out


class _Miscounting(LLMProvider):
    """A provider that breaks the one-to-one promise of batch_complete."""

    name = "miscounting"

    async def complete(self, messages: list[Message], **kwargs: Any) -> CompletionResult:
        raise NotImplementedError

    async def batch_complete(
        self, batches: list[list[Message]], **kwargs: Any
    ) -> list[CompletionResult]:
        fake = FakeLLM()
        results = await fake.batch_complete(batches, **kwargs)
        return results[:-1]


class _Mismatched(LLMProvider):
    """A provider that says ok and returns something the draft cannot hold."""

    name = "mismatched"

    async def complete(self, messages: list[Message], **kwargs: Any) -> CompletionResult:
        raise NotImplementedError

    async def batch_complete(
        self, batches: list[list[Message]], **kwargs: Any
    ) -> list[CompletionResult]:
        return [
            CompletionResult(ok=True, parsed={"km_explanation": "", "target_sentences": []})
            for _ in batches
        ]


async def test_a_failed_concept_is_recorded_not_dropped() -> None:
    """PRD 6.1 sends failures round again, which needs to know which they were."""
    seed = load_seed_file(HSK1)

    draft = await run(seed, _Failing())

    assert [f.slug for f in draft.failures] == ["zh.hsk1.locative_nouns"]
    assert draft.failures[0].error_code == "llm.timeout"
    assert len(draft.concepts) == len(seed.concepts) - 1
    assert "zh.hsk1.locative_nouns" not in {c.slug for c in draft.concepts}
    assert draft.complete is False


async def test_output_that_does_not_fit_the_draft_is_a_failure_not_a_half_concept() -> None:
    draft = await run(a_seed(), _Mismatched())

    assert [f.error_code for f in draft.failures] == ["draft.schema_mismatch"]
    assert draft.concepts == ()


async def test_a_provider_that_loses_a_result_is_refused_rather_than_misfiled() -> None:
    """Off-by-one in the provider would otherwise shift every later concept."""
    seed = load_seed_file(HSK1)

    with pytest.raises(RuntimeError, match="one-to-one"):
        await run(seed, _Miscounting())


# ------------------------------------------------------------------------ CLI


def test_a_billed_provider_will_not_run_without_being_told_to(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """CLAUDE.md section 6: spending money stops and asks (D-085)."""
    called = False

    def _never(**kwargs: Any) -> LLMProvider:
        nonlocal called
        called = True
        return FakeLLM()

    monkeypatch.setattr(generate_module, "get_settings", lambda: settings(llm_provider="openai"))
    monkeypatch.setattr(generate_module, "get_llm", _never)

    exit_code = main(["--out", str(tmp_path / "draft.json")])

    assert exit_code == 2
    assert "--confirm-spend" in capsys.readouterr().err
    assert called is False


def test_the_fake_provider_runs_without_ceremony(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(generate_module, "get_settings", lambda: settings())
    out = tmp_path / "draft.json"

    exit_code = main(["--limit", "2", "--out", str(out)])

    assert exit_code == 0
    assert out.exists()
    assert "2 concept(s) drafted by fake" in capsys.readouterr().out


def test_the_written_draft_reloads_as_a_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """build_pack.py reads this file back; round-tripping is the contract."""
    monkeypatch.setattr(generate_module, "get_settings", lambda: settings())
    out = tmp_path / "draft.json"

    main(["--limit", "2", "--out", str(out)])
    reloaded = Draft.model_validate_json(out.read_text(encoding="utf-8"))

    assert len(reloaded.concepts) == 2
    assert reloaded.concepts[0].slug == "zh.hsk1.locative_nouns"


def test_a_bad_seed_stops_before_a_single_call_is_made(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(generate_module, "get_settings", lambda: settings())
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nmeta: {}\nconcepts: []\n", encoding="utf-8")

    exit_code = main(["--seed", str(bad), "--out", str(tmp_path / "draft.json")])

    assert exit_code == 1
    assert "meta.language" in capsys.readouterr().err


# ------------------------------------------------- what the draft itself refuses
#
# Everything above drives the draft through FakeLLM, which always produces a
# well-formed one — so none of it would notice the draft's own rules being
# relaxed. Two mutations survived on exactly that (removing min_length from
# sources, and from dialogues) before these existed.


def _a_draft(**overrides: Any) -> dict[str, Any]:
    return {
        "language": "zh",
        "level": "HSK1",
        "licence": "public standards",
        "sources": [{"id": "zh.standard.2021"}],
        "seed_file": "test.yaml",
        "provider": "fake",
        "model": "fake",
        "generated_at": "2026-09-19T00:00:00Z",
        "usage": {},
        "concepts": [],
        **overrides,
    }


def _a_concept(**overrides: Any) -> dict[str, Any]:
    return {
        "slug": "zh.hsk1.want_noun",
        "pattern": "我要 + [名词]",
        "hskk_task_types": ["listen_and_repeat"],
        "km_explanation": "ខ្ញុំ",
        "target_sentences": [{"zh": "我要水。", "pinyin": "wǒ yào shuǐ.", "km_gloss": "ខ្ញុំ"}],
        "substitutions": [{"zh": "水", "pinyin": "shuǐ", "km_gloss": "ទឹក"}],
        "dialogues": [
            {
                "hskk_task_type": "listen_and_repeat",
                "prompt_zh": "你要水吗",
                "prompt_km": "អ្វី",
                "sample_answer_zh": "我要水。",
            }
        ],
        **overrides,
    }


def test_a_draft_without_sources_cannot_be_built() -> None:
    """R7 again, one layer down: provenance is not optional (PRD 5.5)."""
    with pytest.raises(ValidationError):
        Draft.model_validate(_a_draft(sources=[]))


def test_a_draft_without_a_licence_cannot_be_built() -> None:
    with pytest.raises(ValidationError):
        Draft.model_validate(_a_draft(licence=""))


@pytest.mark.parametrize(
    "emptied", ["target_sentences", "substitutions", "dialogues", "hskk_task_types"]
)
def test_a_concept_draft_missing_a_whole_section_is_refused(emptied: str) -> None:
    """A concept with no dialogues is not a thin concept, it is a broken one."""
    with pytest.raises(ValidationError):
        ConceptDraft.model_validate(_a_concept(**{emptied: []}))


def test_a_well_formed_concept_draft_is_accepted() -> None:
    """The counterpart: the refusals above are not refusing everything."""
    concept = ConceptDraft.model_validate(_a_concept())

    assert concept.dialogues[0].hskk_task_type == "listen_and_repeat"


# ------------------------------------------------- the counts, said twice
#
# D-104. minItems is a schema keyword a provider may reject or ignore — strict
# structured output takes only a subset of JSON Schema and which subset has
# moved — so the requirement is also stated in the prompt, where no provider
# can drop it. Neither is the enforcement: validate.py is.


def test_the_prompt_states_how_much_to_write() -> None:
    seed = a_seed()

    messages = concept_messages(
        seed.concepts[0], seed=seed, min_sentences=8, min_substitutions=12, min_dialogues=3
    )
    said = " ".join(message.content for message in messages)

    assert "8 target sentences" in said
    assert "12 substitution items" in said
    assert "3 dialogue tasks" in said


def test_a_run_tells_the_model_the_configured_counts() -> None:
    """The prompt and the schema have to agree, and both come from config."""
    seed = a_seed()
    configured = settings(
        pipeline_min_sentences_per_concept=6,
        pipeline_min_substitutions_per_concept=9,
        pipeline_min_dialogues_per_concept=2,
    )
    recorded: list[str] = []

    class _Recording(FakeLLM):
        async def batch_complete(self, batches: list[list[Message]], **kwargs: Any) -> Any:
            recorded.extend(m.content for batch in batches for m in batch)
            return await super().batch_complete(batches, **kwargs)

    draft = asyncio.run(
        generate(
            seed,
            llm=_Recording(),
            settings=configured,
            seed_file=Path("test.yaml"),
            now=dt.datetime(2026, 10, 13, tzinfo=dt.UTC),
        )
    )

    assert "6 target sentences" in " ".join(recorded)
    assert len(draft.concepts[0].target_sentences) == 6  # the schema says so too


def test_the_counts_are_left_out_when_nobody_asks_for_them() -> None:
    """concept_messages is also called in tests and tools that do not care."""
    seed = a_seed()

    said = " ".join(m.content for m in concept_messages(seed.concepts[0], seed=seed))

    assert "at least" not in said


# ------------------------------------------------- what the prompt has to carry
#
# The first run against a real provider produced 31 violations on one concept.
# Sixteen were validate.py's own bug; the rest were the prompt not saying what
# it needed. These are that gap, held open.


def test_the_level_vocabulary_reaches_the_model() -> None:
    """Rule 1 said "at or below the stated level" and never said what that was.

    The model guessed generously — 椅 猫 瓶 狗 室 桌 停 公园 箱 in HSK1
    sentences — and every one was rejected downstream.
    """
    from pipeline.wordlist import load_wordlist

    hsk1 = load_wordlist("zh", "HSK1")
    prompt = system_prompt(hsk1)

    assert "水" in prompt
    assert "椅" not in prompt
    assert prompt.rstrip().endswith("and nothing else.")


def test_without_a_word_list_the_prompt_still_reads_as_instructions() -> None:
    assert system_prompt(None).rstrip().endswith("and nothing else.")
    assert "vocabulary of this level" not in system_prompt(None)


def test_a_level_with_no_word_list_is_refused_before_any_money_is_spent() -> None:
    """wordlist.py asks every caller not to read a missing list as "nothing to
    check". Generating anyway bills for a draft rule 1 will reject."""
    from pipeline.wordlist import MissingWordlistError

    document: dict[str, Any] = {
        "schema_version": 1,
        "meta": {
            "language": "zh",
            "level": "HSK9",
            "licence": "public standards",
            "sources": [{"id": "zh.standard.2021"}],
        },
        "concepts": [
            {
                "slug": "zh.hsk9.whatever",
                "standard_ref": "九01",
                "pattern": "X + Y",
                "km_explanation": "[[km:concept.zh.hsk9.whatever]]",
                "hskk_task_types": ["listen_and_repeat"],
                "sort_order": 10,
            }
        ],
    }
    seed = parse_seed(document, origin=Path("test.yaml"))

    # Specifically the missing list, not merely "something raised": HSK9 has
    # no configured character limit either, and a loose `raises` here passed
    # even with the refusal deleted.
    with pytest.raises(MissingWordlistError):
        asyncio.run(run(seed, FakeLLM()))


def test_the_character_limit_is_stated_and_covers_dialogue_prompts() -> None:
    """The one string the first run broke it on was not a sentence: a dialogue
    prompt whose instruction wording (an opening "please say after me") counts toward the limit."""
    seed = a_seed()
    messages = concept_messages(seed.concepts[0], seed=seed, max_chars=8)
    user = next(m.content for m in messages if m.role == "user")

    assert "8 Chinese characters" in user
    assert "dialogue prompt" in user


def test_the_character_limit_is_not_written_into_the_prompt_file() -> None:
    """R3: the number travels from PIPELINE_MAX_CHARS_*, so a different limit
    has to show up in the text."""
    seed = a_seed()
    user = next(
        m.content
        for m in concept_messages(seed.concepts[0], seed=seed, max_chars=18)
        if m.role == "user"
    )

    assert "18 Chinese characters" in user

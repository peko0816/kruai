"""The eight rules of PRD 6.2, one passing case and one failing case each.

That pairing is BACKLOG E4's acceptance criterion, and it is the shape that
matters: a rule tested only on bad input can be one that rejects everything,
and a rule tested only on good input can be one that rejects nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from pipeline import validate as validate_module
from pipeline.draft import ConceptDraft, Draft
from pipeline.validate import (
    Violation,
    check_coverage,
    check_duplicates,
    check_hskk,
    check_khmer,
    check_length,
    check_pinyin,
    check_sources,
    check_wordlist,
    main,
    max_characters,
    validate,
)
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
    }
    return Settings(**{**base, **overrides})


def a_wordlist(*words: str) -> Wordlist:
    return Wordlist(language="zh", level="HSK1", words=frozenset(words or {"我", "要", "水"}))


def rules(problems: list[Violation]) -> list[str]:
    return [problem.rule for problem in problems]


# ------------------------------------------------------- rule 1: 词表越界


def test_a_sentence_inside_the_vocabulary_passes() -> None:
    assert check_wordlist("我要水。", a_wordlist(), where="x") == []


def test_a_word_above_the_level_is_refused() -> None:
    problems = check_wordlist("我要咖啡。", a_wordlist(), where="x")

    assert rules(problems) == ["wordlist"]
    assert "咖啡" in problems[0].message


# ----------------------------------------------------------- rule 2: 长度


def test_a_sentence_within_the_length_limit_passes() -> None:
    assert check_length("我要水。", maximum=8, where="x") == []


def test_a_sentence_over_the_length_limit_is_refused() -> None:
    problems = check_length("我要一杯很热的牛奶和面包。", maximum=8, where="x")

    assert rules(problems) == ["length"]


def test_punctuation_does_not_count_towards_the_limit() -> None:
    """Eight characters is eight characters to say, not eight glyphs to print."""
    assert check_length("我要水，你要水！", maximum=8, where="x") == []


def test_every_configured_level_has_a_limit_and_others_are_refused() -> None:
    assert max_characters("HSK1", settings()) == 8
    assert max_characters("HSK3", settings()) == 18

    with pytest.raises(ValueError, match="no PIPELINE_MAX_CHARS"):
        max_characters("HSK4", settings())


# ------------------------------------------------------- rule 3: 拼音与声调


def test_pinyin_that_matches_the_characters_passes() -> None:
    assert check_pinyin("我要水", "wǒ yào shuǐ", where="x") == []


def test_pinyin_that_does_not_match_is_refused() -> None:
    problems = check_pinyin("我要水", "wǒ yào chá", where="x")

    assert rules(problems) == ["pinyin"]
    assert "syllable 3" in problems[0].message


def test_a_missing_syllable_is_refused() -> None:
    assert rules(check_pinyin("我要水", "wǒ yào", where="x")) == ["pinyin"]


def test_a_legitimate_heteronym_reading_is_accepted() -> None:
    """了 is le and liǎo; rejecting either would fail on every second sentence."""
    assert check_pinyin("他买了书", "tā mǎi le shū", where="x") == []
    assert check_pinyin("他买了书", "tā mǎi liǎo shū", where="x") == []


def test_tone_marks_are_what_is_compared() -> None:
    """D-087: diacritics, not digits. Same syllable, wrong tone, still refused."""
    assert rules(check_pinyin("我要水", "wǒ yáo shuǐ", where="x")) == ["pinyin"]


# ----------------------------------------------- rule 4: 高棉语解释非空且非中文


def test_khmer_text_passes() -> None:
    assert check_khmer(KM, where="x") == []


@pytest.mark.parametrize(
    "text", ["I want water", "我要水", "   ", "[[km:concept.zh.hsk1.want_noun]]"]
)
def test_a_khmer_field_that_is_not_khmer_is_refused(text: str) -> None:
    assert rules(check_khmer(text, where="x")) == ["khmer"]


def test_a_placeholder_is_refused_as_a_placeholder_not_as_missing_khmer() -> None:
    """The two branches say different things and want different fixes.

    A mutation survived on this: the placeholder string carries no Khmer
    either, so removing the placeholder check left the other branch reporting
    it anyway and every assertion still passed.
    """
    problems = check_khmer("[[km:concept.zh.hsk1.want_noun]]", where="x")

    assert "placeholder" in problems[0].message


def test_a_placeholder_left_inside_real_khmer_is_still_refused() -> None:
    """The case the other branch cannot catch: half-written Khmer."""
    problems = check_khmer(f"{KM} [[km:concept.zh.hsk1.want_noun]]", where="x")

    assert rules(problems) == ["khmer"]
    assert "placeholder" in problems[0].message


def test_khmer_quoting_chinese_is_still_khmer() -> None:
    assert check_khmer(f"「我要」 {KM}", where="x") == []


# ------------------------------------------------------- rule 5: concept 覆盖


def a_concept(**overrides: Any) -> ConceptDraft:
    payload: dict[str, Any] = {
        "slug": "zh.hsk1.want_noun",
        "pattern": "我要 + [名词]",
        "hskk_task_types": ["listen_and_repeat"],
        "km_explanation": KM,
        "target_sentences": [{"zh": "我要水。", "pinyin": "wǒ yào shuǐ", "km_gloss": KM}],
        "substitutions": [{"zh": "水", "pinyin": "shuǐ", "km_gloss": KM}],
        "dialogues": [
            {
                "hskk_task_type": "listen_and_repeat",
                "prompt_zh": "你要水",
                "prompt_km": KM,
                "sample_answer_zh": "我要水",
            }
        ],
        **overrides,
    }
    return ConceptDraft.model_validate(payload)


def test_a_concept_with_enough_material_passes() -> None:
    assert (
        check_coverage(
            a_concept(), min_sentences=1, min_substitutions=1, min_dialogues=1, where="x"
        )
        == []
    )


def test_a_concept_with_too_little_material_is_refused() -> None:
    problems = check_coverage(
        a_concept(), min_sentences=8, min_substitutions=12, min_dialogues=3, where="x"
    )

    assert rules(problems) == ["coverage", "coverage", "coverage"]
    assert "1 target sentences, needs 8" in problems[0].message


# ----------------------------------------------------- rule 6: HSKK 任务类型


def test_a_concept_graded_as_a_task_type_passes() -> None:
    assert check_hskk(a_concept(), where="x") == []


def test_a_dialogue_of_a_type_the_concept_is_not_graded_as_is_refused() -> None:
    concept = a_concept(
        hskk_task_types=["listen_and_repeat"],
        dialogues=[
            {
                "hskk_task_type": "read_aloud",
                "prompt_zh": "你要水",
                "prompt_km": KM,
                "sample_answer_zh": "我要水",
            }
        ],
    )

    problems = check_hskk(concept, where="x")

    assert rules(problems) == ["hskk"]
    assert "read_aloud" in problems[0].message


# ---------------------------------------------------------------- rule 7: 去重


def test_distinct_sentences_pass() -> None:
    labelled = [("a", "我要水。"), ("b", "他买了三本书。")]

    assert check_duplicates(labelled, threshold=0.9) == []


def test_near_identical_sentences_are_refused() -> None:
    labelled = [("a", "我要一杯水。"), ("b", "我要一杯水！")]

    problems = check_duplicates(labelled, threshold=0.9)

    assert rules(problems) == ["duplicate"]
    assert "b" in problems[0].message


# -------------------------------------------------------------- rule 8: 来源合规


def test_a_draft_that_names_its_sources_passes() -> None:
    assert check_sources([{"id": "zh.standard.2021"}], "public standards", where="draft") == []


@pytest.mark.parametrize(
    ("sources", "licence"),
    [([], "public standards"), ([{"id": "zh.standard.2021"}], "  "), ([{"id": ""}], "ok")],
)
def test_content_without_provenance_is_refused(sources: list[dict[str, str]], licence: str) -> None:
    assert rules(check_sources(sources, licence, where="draft")) == ["sources"]


# ------------------------------------------------------------------- the runner


def a_draft(**overrides: Any) -> Draft:
    payload: dict[str, Any] = {
        "language": "zh",
        "level": "HSK1",
        "licence": "public standards",
        "sources": [{"id": "zh.standard.2021"}],
        "seed_file": "test.yaml",
        "provider": "fake",
        "model": "fake",
        "generated_at": "2026-09-19T00:00:00Z",
        "usage": {},
        "concepts": [a_concept().model_dump()],
        **overrides,
    }
    return Draft.model_validate(payload)


def test_a_clean_draft_produces_no_violations() -> None:
    configured = settings(
        pipeline_min_sentences_per_concept=1,
        pipeline_min_substitutions_per_concept=1,
        pipeline_min_dialogues_per_concept=1,
    )

    problems = validate(a_draft(), settings=configured, wordlist=a_wordlist("我", "要", "水", "你"))

    assert problems == []


def test_the_runner_reaches_every_rule() -> None:
    """One draft, broken eight ways: each rule has to report from inside a run."""
    configured = settings(
        pipeline_min_sentences_per_concept=9,  # coverage
        pipeline_min_substitutions_per_concept=1,
        pipeline_min_dialogues_per_concept=1,
        pipeline_max_chars_hsk1=2,  # length
    )
    broken = a_concept(
        km_explanation="written in English",  # khmer
        hskk_task_types=["listen_and_repeat"],
        target_sentences=[
            {"zh": "我要咖啡。", "pinyin": "wǒ yào chá", "km_gloss": KM},  # wordlist + pinyin
            {"zh": "我要咖啡。", "pinyin": "wǒ yào kā fēi", "km_gloss": KM},  # duplicate
        ],
        dialogues=[
            {
                "hskk_task_type": "read_aloud",  # hskk
                "prompt_zh": "你要水",
                "prompt_km": KM,
                "sample_answer_zh": "我要水",
            }
        ],
    )

    problems = validate(
        a_draft(concepts=[broken.model_dump()], sources=[{"id": ""}]),  # sources
        settings=configured,
        wordlist=a_wordlist("我", "要", "水", "你"),
    )

    assert set(rules(problems)) == {
        "wordlist",
        "length",
        "pinyin",
        "khmer",
        "coverage",
        "hskk",
        "duplicate",
        "sources",
    }


def test_the_runner_names_the_concept_a_violation_came_from() -> None:
    configured = settings(
        pipeline_min_sentences_per_concept=1,
        pipeline_min_substitutions_per_concept=1,
        pipeline_min_dialogues_per_concept=1,
    )

    problems = validate(
        a_draft(concepts=[a_concept(km_explanation="English").model_dump()]),
        settings=configured,
        wordlist=a_wordlist("我", "要", "水", "你"),
    )

    assert problems[0].where.startswith("concepts[0](zh.hsk1.want_noun)")


# ------------------------------------------------------------------------ CLI


def write_wordlist(tmp_path: Path, *words: str) -> Path:
    directory = tmp_path / "zh"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "hsk1.txt").write_text(
        "# source: test fixture\n" + "\n".join(words), encoding="utf-8"
    )
    return tmp_path


def test_the_cli_refuses_a_draft_it_has_no_wordlist_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A rule cannot pass because its data is missing; that is worse than failing."""
    monkeypatch.setattr(validate_module, "get_settings", settings)
    monkeypatch.setattr(validate_module, "WORDLIST_DIR", tmp_path / "empty", raising=False)
    monkeypatch.setattr("pipeline.wordlist.WORDLIST_DIR", tmp_path / "empty")
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(a_draft().model_dump(mode="json")), encoding="utf-8")

    exit_code = main([str(draft)])

    assert exit_code == 2
    assert "no word list" in capsys.readouterr().err


def test_the_cli_reports_violations_and_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        validate_module,
        "get_settings",
        lambda: settings(
            pipeline_min_sentences_per_concept=1,
            pipeline_min_substitutions_per_concept=1,
            pipeline_min_dialogues_per_concept=1,
        ),
    )
    monkeypatch.setattr("pipeline.wordlist.WORDLIST_DIR", write_wordlist(tmp_path, "我", "要"))
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(a_draft().model_dump(mode="json")), encoding="utf-8")

    exit_code = main([str(draft)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "[wordlist]" in captured.err
    assert "violation(s)" in captured.out


def test_the_cli_accepts_a_clean_draft(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        validate_module,
        "get_settings",
        lambda: settings(
            pipeline_min_sentences_per_concept=1,
            pipeline_min_substitutions_per_concept=1,
            pipeline_min_dialogues_per_concept=1,
        ),
    )
    monkeypatch.setattr(
        "pipeline.wordlist.WORDLIST_DIR", write_wordlist(tmp_path, "我", "要", "水", "你")
    )
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(a_draft().model_dump(mode="json")), encoding="utf-8")

    exit_code = main([str(draft)])

    assert exit_code == 0
    assert "no violations" in capsys.readouterr().out


def test_a_sample_answer_may_repeat_a_drill_sentence() -> None:
    """D-088: kinds are compared with their own kind, not with each other.

    A dialogue answering with the sentence the learner has just practised is
    the lesson working, not duplicated content.
    """
    configured = settings(
        pipeline_min_sentences_per_concept=1,
        pipeline_min_substitutions_per_concept=1,
        pipeline_min_dialogues_per_concept=1,
    )
    concept = a_concept(
        target_sentences=[{"zh": "我要水。", "pinyin": "wǒ yào shuǐ", "km_gloss": KM}],
        dialogues=[
            {
                "hskk_task_type": "listen_and_repeat",
                "prompt_zh": "你要水",
                "prompt_km": KM,
                "sample_answer_zh": "我要水",
            }
        ],
    )

    problems = validate(
        a_draft(concepts=[concept.model_dump()]),
        settings=configured,
        wordlist=a_wordlist("我", "要", "水", "你"),
    )

    assert problems == []


def test_two_drill_sentences_that_differ_by_punctuation_are_still_refused() -> None:
    """The other half of D-088: within a kind, near-identical is still waste."""
    configured = settings(
        pipeline_min_sentences_per_concept=1,
        pipeline_min_substitutions_per_concept=1,
        pipeline_min_dialogues_per_concept=1,
    )
    concept = a_concept(
        target_sentences=[
            {"zh": "我要水。", "pinyin": "wǒ yào shuǐ", "km_gloss": KM},
            {"zh": "我要水！", "pinyin": "wǒ yào shuǐ", "km_gloss": KM},
        ]
    )

    problems = validate(
        a_draft(concepts=[concept.model_dump()]),
        settings=configured,
        wordlist=a_wordlist("我", "要", "水", "你"),
    )

    assert rules(problems) == ["duplicate"]

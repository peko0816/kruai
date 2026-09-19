"""The seed checker, run against seeds built to be wrong (BACKLOG E1).

E1's acceptance criterion is "an invalid seed is refused and the offending
field named", so nearly every test here builds one specific defect and insists
both halves happen: refused, and the field path in the message.

The two tests that assert the repository is currently clean are marked as such.
They are worth having and they prove nothing about the checker — `== []` passes
just as well when the checker has stopped looking, which is what the D7
mutations showed on exactly that shape of assertion.
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from pipeline import validate_seed
from pipeline.seed_schema import (
    SeedError,
    SeedProblem,
    check_seed_tree,
    load_seed_file,
    parse_seed,
    seed_files_under,
)

ORIGIN = Path("hsk1.yaml")


def a_seed() -> dict[str, Any]:
    """A minimal valid seed file, as a parsed document."""
    return {
        "schema_version": 1,
        "meta": {
            "language": "zh",
            "level": "HSK1",
            "licence": "Derived from public standards and syllabuses.",
            "sources": [{"id": "zh.standard.2021", "detail": "level 1 grammar"}],
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
    }


def refuse(document: dict[str, Any]) -> list[SeedProblem]:
    """Validate something expected to be invalid, and return why."""
    with pytest.raises(SeedError) as raised:
        parse_seed(document, origin=ORIGIN)
    return raised.value.problems


def fields(problems: list[SeedProblem]) -> list[str]:
    return [problem.field for problem in problems]


def rendered(problems: list[SeedProblem]) -> str:
    return "\n".join(str(problem) for problem in problems)


# ------------------------------------------------------------- the happy path


def test_a_minimal_seed_parses() -> None:
    seed = parse_seed(a_seed(), origin=ORIGIN)

    assert seed.meta.level == "HSK1"
    assert seed.concepts[0].slug == "zh.hsk1.want_noun"
    assert seed.concepts[0].hskk_task_types == ("listen_and_repeat",)


def test_a_khmer_explanation_is_accepted_and_is_not_counted_as_pending() -> None:
    document = a_seed()
    document["concepts"][0]["km_explanation"] = "ខ្ញុំ"

    seed = parse_seed(document, origin=ORIGIN)

    assert seed.awaiting_translation == ()


def test_a_placeholder_explanation_is_accepted_and_counted_as_pending() -> None:
    """The M1 gate needs to know; a pull request does not fail on it (D-079)."""
    seed = parse_seed(a_seed(), origin=ORIGIN)

    assert seed.awaiting_translation == ("zh.hsk1.want_noun",)


# ------------------------------------------------------------ structure rules


def test_an_unknown_field_is_refused_by_name() -> None:
    """extra="forbid": a misspelt key must not be silently ignored."""
    document = a_seed()
    document["concepts"][0]["km_explaination"] = "typo"

    problems = refuse(document)

    assert "concepts[0].km_explaination" in fields(problems)


@pytest.mark.parametrize(
    ("removed", "expected_field"),
    [
        ("schema_version", "schema_version"),
        ("meta", "meta"),
        ("concepts", "concepts"),
    ],
)
def test_a_missing_top_level_key_is_refused_by_name(removed: str, expected_field: str) -> None:
    document = a_seed()
    del document[removed]

    assert expected_field in fields(refuse(document))


def test_a_file_with_no_concepts_is_refused() -> None:
    document = a_seed()
    document["concepts"] = []

    assert "'concepts' is empty" in rendered(refuse(document))


def test_a_future_schema_version_is_refused_rather_than_guessed_at() -> None:
    document = a_seed()
    document["schema_version"] = 2

    assert "schema_version" in fields(refuse(document))


@pytest.mark.parametrize(
    "slug",
    ["want_noun", "zh.hsk1", "ZH.HSK1.want_noun", "zh.hsk1.want-noun", "zh.hsk1.想要"],
)
def test_a_malformed_slug_is_refused_by_name(slug: str) -> None:
    document = a_seed()
    document["concepts"][0]["slug"] = slug

    problems = refuse(document)

    assert "concepts[0].slug" in fields(problems)
    assert "zh.hsk1.want_noun" in rendered(problems)  # the message shows the shape


def test_a_slug_from_another_level_is_refused() -> None:
    """The commonest real mistake: a concept pasted into the wrong file."""
    document = a_seed()
    document["concepts"][0]["slug"] = "zh.hsk2.want_noun"

    assert "zh.hsk1." in rendered(refuse(document))


def test_a_slug_from_another_language_is_refused() -> None:
    document = a_seed()
    document["concepts"][0]["slug"] = "en.hsk1.want_noun"

    assert "prefix" in rendered(refuse(document))


def test_two_concepts_with_the_same_slug_are_refused() -> None:
    document = a_seed()
    document["concepts"].append(deepcopy(document["concepts"][0]))
    document["concepts"][1].update(sort_order=20, standard_ref="一37")

    assert "concepts[1].slug" in rendered(refuse(document))


def test_two_concepts_claiming_the_same_position_are_refused() -> None:
    """Otherwise teaching order is whatever the database happens to return."""
    document = a_seed()
    second = deepcopy(document["concepts"][0])
    second.update(slug="zh.hsk1.have_noun", standard_ref="一37")
    document["concepts"].append(second)

    assert "concepts[1].sort_order" in rendered(refuse(document))


@pytest.mark.parametrize("ref", ["1", "一1", "一001", "A01", "一0a"])
def test_a_malformed_syllabus_reference_is_refused_by_name(ref: str) -> None:
    document = a_seed()
    document["concepts"][0]["standard_ref"] = ref

    assert "concepts[0].standard_ref" in fields(refuse(document))


def test_a_concept_may_omit_the_syllabus_reference() -> None:
    """Not every source numbers its items; the BCT scenario list does not."""
    document = a_seed()
    document["concepts"][0].pop("standard_ref", None)

    assert parse_seed(document, origin=ORIGIN).concepts[0].standard_ref == ""


def test_two_concepts_claiming_the_same_syllabus_item_are_refused() -> None:
    """Otherwise a copy-paste silently drops one grammar point from the level."""
    document = a_seed()
    document["concepts"][0]["standard_ref"] = "一01"
    second = deepcopy(document["concepts"][0])
    second.update(slug="zh.hsk1.have_noun", sort_order=20)
    document["concepts"].append(second)

    assert "concepts[1].standard_ref" in rendered(refuse(document))


def test_concepts_without_a_syllabus_reference_do_not_collide() -> None:
    document = a_seed()
    document["concepts"][0].pop("standard_ref")
    second = deepcopy(document["concepts"][0])
    second.update(slug="zh.hsk1.have_noun", sort_order=20)
    document["concepts"].append(second)

    assert len(parse_seed(document, origin=ORIGIN).concepts) == 2


def test_a_negative_sort_order_is_refused() -> None:
    document = a_seed()
    document["concepts"][0]["sort_order"] = -1

    assert "concepts[0].sort_order" in fields(refuse(document))


@pytest.mark.parametrize("level", ["HSK0", "HSK10", "hsk1", "Level One", ""])
def test_a_level_that_is_not_an_hsk_level_is_refused(level: str) -> None:
    document = a_seed()
    document["meta"]["level"] = level
    document["concepts"][0]["slug"] = "zh.x.want_noun"  # keep the prefix rule quiet

    assert "meta" in rendered(refuse(document))


def test_an_unknown_language_is_refused() -> None:
    document = a_seed()
    document["meta"]["language"] = "km"

    assert "meta.language" in fields(refuse(document))


# ------------------------------------------------------- the content-specific rules


@pytest.mark.parametrize(
    "explanation",
    [
        "I want something.",  # English in a Khmer field
        "我要 + 名词",  # the explanation written in Chinese (PRD 6.2)
        "[[km:unfinished",  # a placeholder that is not one
        "   ",  # blank
    ],
)
def test_an_explanation_that_is_not_khmer_is_refused_by_name(explanation: str) -> None:
    document = a_seed()
    document["concepts"][0]["km_explanation"] = explanation

    assert "concepts[0].km_explanation" in fields(refuse(document))


def test_khmer_quoting_the_chinese_pattern_is_accepted() -> None:
    """Presence of Khmer, not absence of Han: quoting the pattern is good teaching."""
    document = a_seed()
    document["concepts"][0]["km_explanation"] = "「我要」 ខ្ញុំចង់បាន"

    assert parse_seed(document, origin=ORIGIN).awaiting_translation == ()


def test_a_chinese_concept_without_an_hskk_task_type_is_refused() -> None:
    """PRD 6.2: without one, nothing outside our own judgement grades a turn."""
    document = a_seed()
    document["concepts"][0]["hskk_task_types"] = []

    assert "hskk_task_types" in rendered(refuse(document))


def test_an_hskk_task_type_that_is_not_in_the_syllabus_is_refused() -> None:
    document = a_seed()
    document["concepts"][0]["hskk_task_types"] = ["listen_and_repeet"]

    assert "concepts[0].hskk_task_types[0]" in fields(refuse(document))


def test_an_english_concept_may_not_claim_an_hskk_task_type() -> None:
    document = a_seed()
    document["meta"].update(language="en", level="A1")
    document["concepts"][0]["slug"] = "en.a1.want_noun"

    assert "HSKK grades Chinese only" in rendered(refuse(document))


def test_an_english_concept_without_an_hskk_task_type_is_accepted() -> None:
    document = a_seed()
    document["meta"].update(language="en", level="A1")
    document["meta"]["sources"] = [{"id": "en.gse.toolkit"}]
    document["concepts"][0].update(slug="en.a1.want_noun", hskk_task_types=[])

    assert parse_seed(document, origin=ORIGIN).meta.language == "en"


def test_an_l1_error_whose_two_sentences_are_identical_is_refused() -> None:
    document = a_seed()
    document["concepts"][0]["common_l1_errors"] = [{"wrong": "我要书", "right": "我要书"}]

    assert "concepts[0].common_l1_errors[0]" in fields(refuse(document))


def test_a_blank_example_sentence_is_refused() -> None:
    document = a_seed()
    document["concepts"][0]["example_sentences"] = ["我要水。", "  "]

    assert "concepts[0].example_sentences" in fields(refuse(document))


# ------------------------------------------------------------------- red line R7


def test_a_source_that_is_not_on_the_whitelist_is_refused() -> None:
    document = a_seed()
    document["meta"]["sources"] = [{"id": "zh.some.textbook"}]

    problems = refuse(document)

    assert "meta.sources[0].id" in fields(problems)
    assert "R7" in rendered(problems)


def test_a_file_with_no_sources_is_refused() -> None:
    """PRD 5.5: sources and licence are non-empty, checked again at import."""
    document = a_seed()
    document["meta"]["sources"] = []

    assert "'sources' is empty" in rendered(refuse(document))


def test_a_blank_licence_is_refused() -> None:
    document = a_seed()
    document["meta"]["licence"] = "   "

    assert "meta.licence" in fields(refuse(document))


def test_a_cc_by_source_without_an_attribution_line_is_refused() -> None:
    document = a_seed()
    document["meta"]["sources"] = [{"id": "corpus.tatoeba"}]

    problems = refuse(document)

    assert "meta.sources[0]" in fields(problems)
    assert "credited" in rendered(problems)


def test_a_cc_by_source_with_an_attribution_line_is_accepted() -> None:
    document = a_seed()
    document["meta"]["sources"] = [{"id": "corpus.tatoeba", "attribution": "Tatoeba, CC-BY 2.0 FR"}]

    assert parse_seed(document, origin=ORIGIN).meta.sources[0].id == "corpus.tatoeba"


#: A line somebody might honestly write while recording where a sentence came
#: from. The source id beside it is legitimate, so the whitelist lets it past.
_TEXTBOOK_MENTION = "改写自《HSK标准教程》第 3 课"


def _name_a_textbook_in_the_detail(document: dict[str, Any]) -> str:
    document["meta"]["sources"][0]["detail"] = _TEXTBOOK_MENTION
    return "meta.sources[0].detail"


def _name_a_textbook_in_an_example(document: dict[str, Any]) -> str:
    document["concepts"][0]["example_sentences"] = ["我要水。", _TEXTBOOK_MENTION]
    return "concepts[0].example_sentences[1]"


def _name_a_textbook_in_the_licence(document: dict[str, Any]) -> str:
    document["meta"]["licence"] = _TEXTBOOK_MENTION
    return "meta.licence"


@pytest.mark.parametrize(
    "mutate",
    [
        _name_a_textbook_in_the_detail,
        _name_a_textbook_in_an_example,
        _name_a_textbook_in_the_licence,
    ],
)
def test_a_publisher_textbook_named_anywhere_in_the_file_is_refused(
    mutate: Any,
) -> None:
    """The second net: the source id is fine and the textbook is in the prose."""
    document = a_seed()
    where = mutate(document)

    problems = refuse(document)

    assert where in fields(problems)
    assert "PRD 5.5" in rendered(problems)


def test_a_textbook_is_reported_even_when_the_file_is_also_malformed() -> None:
    """Otherwise the R7 refusal waits behind a typo, and arrives after the work."""
    document = a_seed()
    document["meta"]["sources"][0]["detail"] = _TEXTBOOK_MENTION
    document["concepts"][0]["sort_order"] = -1

    problems = refuse(document)

    assert "meta.sources[0].detail" in fields(problems)
    assert "concepts[0].sort_order" in fields(problems)


# ------------------------------------------------------------------ file input


def write(tmp_path: Path, text: str, name: str = "hsk1.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_duplicate_yaml_key_is_refused_rather_than_silently_dropped(tmp_path: Path) -> None:
    """PyYAML keeps the last one. One of the two explanations would vanish."""
    path = write(
        tmp_path,
        "schema_version: 1\n"
        "meta:\n"
        "  language: zh\n"
        "  level: HSK1\n"
        "  licence: x\n"
        "  sources: [{id: zh.standard.2021}]\n"
        "concepts:\n"
        "  - slug: zh.hsk1.want_noun\n"
        "    pattern: 我要 + [名词]\n"
        "    km_explanation: ខ្ញុំ\n"
        "    km_explanation: I want\n"
        "    hskk_task_types: [listen_and_repeat]\n",
    )

    with pytest.raises(SeedError) as raised:
        load_seed_file(path)

    assert "duplicate key" in str(raised.value)


def test_a_file_that_is_not_yaml_is_refused_with_the_line_number(tmp_path: Path) -> None:
    path = write(tmp_path, "schema_version: 1\nmeta: [unclosed\n")

    with pytest.raises(SeedError) as raised:
        load_seed_file(path)

    assert "not valid YAML" in str(raised.value)


@pytest.mark.parametrize("text", ["", "# only a comment\n", "- a\n- list\n"])
def test_a_file_that_is_not_a_mapping_is_refused(tmp_path: Path, text: str) -> None:
    path = write(tmp_path, text)

    with pytest.raises(SeedError) as raised:
        load_seed_file(path)

    assert "mapping" in str(raised.value)


def test_a_missing_file_is_refused_rather_than_crashing(tmp_path: Path) -> None:
    with pytest.raises(SeedError) as raised:
        load_seed_file(tmp_path / "nope.yaml")

    assert "cannot read" in str(raised.value)


def test_the_same_slug_in_two_files_is_refused(tmp_path: Path) -> None:
    """concepts.slug is UNIQUE; the collision must not wait for import."""
    body = (
        "schema_version: 1\n"
        "meta:\n"
        "  language: zh\n"
        "  level: HSK1\n"
        "  licence: x\n"
        "  sources: [{id: zh.standard.2021}]\n"
        "concepts:\n"
        "  - slug: zh.hsk1.want_noun\n"
        "    pattern: 我要 + [名词]\n"
        "    km_explanation: ខ្ញុំ\n"
        "    hskk_task_types: [listen_and_repeat]\n"
    )
    write(tmp_path, body, "a.yaml")
    write(tmp_path, body, "b.yaml")

    corpus = check_seed_tree(tmp_path)

    assert len(corpus.files) == 2
    assert "already defined in" in rendered(corpus.problems)
    assert "zh.hsk1.want_noun" in rendered(corpus.problems)


def test_partial_files_are_skipped(tmp_path: Path) -> None:
    """A `_work_in_progress.yaml` is not a seed file yet."""
    write(tmp_path, "nonsense: true\n", "_draft.yaml")
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")

    assert seed_files_under(tmp_path) == []
    assert check_seed_tree(tmp_path).problems == []


def test_a_missing_seed_root_is_not_an_error() -> None:
    """Before E2 there is nothing to check, and that is not a failure."""
    assert check_seed_tree(Path("/nonexistent/seed")).problems == []


# ------------------------------------------------------------------------- CLI


def test_the_cli_names_the_file_and_the_field(tmp_path: Path, capsys: Any) -> None:
    path = write(tmp_path, "schema_version: 1\nmeta: {}\nconcepts: []\n")

    exit_code = validate_seed.main([str(path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "meta.language" in captured.err
    assert str(path) in captured.err


def test_the_cli_accepts_a_good_corpus_and_reports_what_is_pending(
    tmp_path: Path, capsys: Any
) -> None:
    write(
        tmp_path,
        "schema_version: 1\n"
        "meta:\n"
        "  language: zh\n"
        "  level: HSK1\n"
        "  licence: x\n"
        "  sources: [{id: zh.standard.2021}]\n"
        "concepts:\n"
        "  - slug: zh.hsk1.want_noun\n"
        "    pattern: 我要 + [名词]\n"
        "    km_explanation: '[[km:concept.zh.hsk1.want_noun]]'\n"
        "    hskk_task_types: [listen_and_repeat]\n",
    )

    exit_code = validate_seed.main([str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "1 seed file(s), 1 concept(s)" in captured.out
    assert "awaiting a Khmer explanation" in captured.out


def test_strict_refuses_the_same_corpus(tmp_path: Path, capsys: Any) -> None:
    """The M1 gate. Unfinished Khmer stops a launch, not a pull request."""
    write(
        tmp_path,
        "schema_version: 1\n"
        "meta:\n"
        "  language: zh\n"
        "  level: HSK1\n"
        "  licence: x\n"
        "  sources: [{id: zh.standard.2021}]\n"
        "concepts:\n"
        "  - slug: zh.hsk1.want_noun\n"
        "    pattern: 我要 + [名词]\n"
        "    km_explanation: '[[km:concept.zh.hsk1.want_noun]]'\n"
        "    hskk_task_types: [listen_and_repeat]\n",
    )

    exit_code = validate_seed.main([str(tmp_path), "--strict"])

    assert exit_code == 1
    assert "M1 gate" in capsys.readouterr().err


# --------------------------------------------------- the repository as it stands
#
# Both of these pass today by having nothing to check. Said out loud because a
# vacuous green is the thing E1 can least afford to mistake for a working
# checker: the tests above are what demonstrate it works.


def test_every_committed_seed_file_is_valid() -> None:
    corpus = check_seed_tree(validate_seed.SEED_ROOT)

    assert corpus.problems == []


def test_the_example_in_the_readme_is_a_valid_seed_file() -> None:
    """Documentation that lies costs more than documentation that is missing."""
    readme = (validate_seed.SEED_ROOT / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", readme, flags=re.DOTALL)

    assert blocks, "the seed README must show the format"
    seed = parse_seed(yaml.safe_load(blocks[0]), origin=validate_seed.SEED_ROOT / "README.md")
    assert seed.concepts[0].slug.startswith(seed.meta.slug_prefix)

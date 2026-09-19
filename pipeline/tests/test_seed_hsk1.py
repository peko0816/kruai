"""The HSK1 seed is complete against the syllabus it claims (BACKLOG E2).

E2's acceptance criterion is "every HSK1 concept has a seed". That is only
checkable because the source list is closed: appendix A of GF 0025-2021
numbers the level-1 grammar points 一01 through 一48 and then starts level 2.
So the test is not "there are enough concepts", it is "these exact 48".

A missing item, a duplicated number, or a concept invented outside the
syllabus all fail here. Which is the point — the alternative is somebody
counting by hand at M1, against a 260-page scan.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from pipeline.seed_schema import SeedFile, load_seed_file
from pipeline.validate_seed import SEED_ROOT

HSK1: Final = SEED_ROOT / "zh-hsk3.0" / "hsk1.yaml"

#: 一01 … 一48. The closed list of level-1 grammar points.
EXPECTED_REFS: Final[tuple[str, ...]] = tuple(f"一{n:02d}" for n in range(1, 49))


@pytest.fixture(scope="module")
def hsk1() -> SeedFile:
    return load_seed_file(HSK1)


def test_the_file_is_where_the_pipeline_looks_for_it() -> None:
    assert HSK1.exists(), f"{HSK1} is the HSK1 seed; generate.py reads it by name"


def test_every_level_one_grammar_point_has_exactly_one_concept(hsk1: SeedFile) -> None:
    refs = [concept.standard_ref for concept in hsk1.concepts]

    assert refs == list(EXPECTED_REFS)


def test_the_file_declares_the_standard_it_was_built_from(hsk1: SeedFile) -> None:
    """R7: the concepts came from the national standard, and the file says so."""
    ids = {source.id for source in hsk1.meta.sources}

    assert "zh.standard.2021" in ids
    assert "zh.hskk.syllabus" in ids


def test_every_concept_names_the_hskk_task_it_is_judged_as(hsk1: SeedFile) -> None:
    """PRD 6.2. Enforced by the schema too; asserted here on the real file."""
    missing = [c.slug for c in hsk1.concepts if not c.hskk_task_types]

    assert missing == []


def test_every_concept_can_be_repeated_back(hsk1: SeedFile) -> None:
    """The D-083 floor: 听后重复 applies to every level-1 point."""
    without = [c.slug for c in hsk1.concepts if "listen_and_repeat" not in c.hskk_task_types]

    assert without == []


def test_the_question_forms_are_the_ones_marked_as_answerable(hsk1: SeedFile) -> None:
    """D-083's other half, stated as data so a drifting edit shows up here."""
    by_ref = {c.standard_ref: c for c in hsk1.concepts}
    question_forms = {"一04", "一33", "一45", "一46", "一47", "一48"}

    answerable = {ref for ref, c in by_ref.items() if "answer_questions" in c.hskk_task_types}

    assert answerable == question_forms


def test_every_concept_carries_examples_for_the_generator(hsk1: SeedFile) -> None:
    thin = [c.slug for c in hsk1.concepts if len(c.example_sentences) < 2]

    assert thin == []


def test_teaching_order_follows_the_syllabus(hsk1: SeedFile) -> None:
    orders = [c.sort_order for c in hsk1.concepts]

    assert orders == sorted(orders)
    assert len(set(orders)) == len(orders)


def test_the_khmer_is_still_owed_and_says_so(hsk1: SeedFile) -> None:
    """Not a gate — `make seed-strict` is (D-079). This states today's truth.

    When a native speaker has been through the file this assertion flips to
    an empty tuple, and that edit is the moment to notice M1 moved.
    """
    assert len(hsk1.awaiting_translation) == len(hsk1.concepts)


def test_no_concept_invents_teaching_content_nobody_verified(hsk1: SeedFile) -> None:
    """common_l1_errors comes from a Khmer speaker and attempt data (PRD 3.2).

    Left empty deliberately at E2. If this ever fails, someone added error
    pairs — which is welcome, and wants a reviewer who reads Khmer.
    """
    invented = [c.slug for c in hsk1.concepts if c.common_l1_errors]

    assert invented == []


def test_the_seed_directory_holds_nothing_else_yet() -> None:
    """HSK2-9 are seeded later; a stray file here would be generated from."""
    files = sorted(p.name for p in Path(HSK1.parent).glob("*.yaml"))

    assert files == ["hsk1.yaml"]

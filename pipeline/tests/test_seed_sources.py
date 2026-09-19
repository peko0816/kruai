"""The source whitelist and the textbook scan — red line R7 (BACKLOG E1).

The registry itself is asserted for the properties a reviewer would otherwise
have to hold in their head: ids are unique, a CC-BY licence is credited, and
nothing forbidden is quietly listed as allowed.
"""

from __future__ import annotations

import pytest

from pipeline import seed_sources


def test_every_id_is_unique() -> None:
    ids = [source.id for source in seed_sources.ALLOWED_SOURCES]

    assert len(ids) == len(set(ids))
    assert len(seed_sources.SOURCES_BY_ID) == len(ids)


def test_every_share_alike_or_attribution_licence_demands_credit() -> None:
    """A CC-BY family licence obliges us to name the source (PRD 5.5)."""
    for source in seed_sources.ALLOWED_SOURCES:
        if source.licence.upper().startswith("CC-BY"):
            assert source.attribution_required, f"{source.id} is {source.licence} and uncredited"


def test_no_forbidden_title_is_on_the_whitelist() -> None:
    for source in seed_sources.ALLOWED_SOURCES:
        assert seed_sources.forbidden_title_in(source.title) is None
        assert seed_sources.forbidden_title_in(source.id) is None


def test_an_unknown_id_has_no_registry_entry() -> None:
    assert seed_sources.known_source("zh.hsk.syllabus") is not None
    assert seed_sources.known_source("zh.standard.2022") is None
    assert seed_sources.known_source("") is None


@pytest.mark.parametrize(
    "text",
    [
        "《HSK标准教程》",
        "HSK标准教程 第一册",
        "改写自 发展汉语 初级口语",
        "Adapted from Integrated Chinese",
        "integrated   chinese, lesson 4",
        "新实用汉语课本",
        "based on 《博雅汉语》",
    ],
)
def test_a_publisher_textbook_is_recognised_however_it_is_written(text: str) -> None:
    assert seed_sources.forbidden_title_in(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "国际中文教育中文水平等级标准",
        "HSKK 初级 task types",
        "Tatoeba sentence pair #123456",
        "MoEYS grade 4 listening exercise, CC-BY 3.0",
        "",
    ],
)
def test_legitimate_provenance_is_not_flagged(text: str) -> None:
    assert seed_sources.forbidden_title_in(text) is None

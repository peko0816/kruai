"""The word list and the segmentation that uses it (BACKLOG E4, rule 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.wordlist import MissingWordlistError, Wordlist, load_wordlist, uncovered


def write(tmp_path: Path, body: str, name: str = "hsk1.txt") -> Path:
    directory = tmp_path / "zh"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_a_list_loads_with_its_provenance(tmp_path: Path) -> None:
    root = write(tmp_path, "# source: GF 0025-2021 table 6, level 1\n我\n要\n\n# a comment\n水\n")

    wordlist = load_wordlist("zh", "HSK1", directory=root)

    assert wordlist.words == frozenset({"我", "要", "水"})
    assert wordlist.sources == ("GF 0025-2021 table 6, level 1",)


def test_a_missing_list_is_an_error_not_an_empty_list(tmp_path: Path) -> None:
    """The rule must not pass because nobody supplied its data."""
    with pytest.raises(MissingWordlistError, match="cannot run without"):
        load_wordlist("zh", "HSK1", directory=tmp_path)


def test_a_list_with_only_comments_is_an_error(tmp_path: Path) -> None:
    root = write(tmp_path, "# source: nothing yet\n")

    with pytest.raises(ValueError, match="holds no words"):
        load_wordlist("zh", "HSK1", directory=root)


# ------------------------------------------------------------- segmentation


def a_wordlist(*words: str) -> Wordlist:
    return Wordlist(language="zh", level="HSK1", words=frozenset(words))


def test_a_sentence_made_of_listed_words_is_fully_covered() -> None:
    assert uncovered("我要水。", a_wordlist("我", "要", "水")) == []


def test_the_longest_listed_word_wins() -> None:
    """Without this, 电影院 would be read as 电影 + 院 and 院 reported as missing."""
    assert uncovered("电影院", a_wordlist("电影", "电影院")) == []


def test_what_the_list_cannot_cover_is_reported() -> None:
    assert uncovered("我要咖啡。", a_wordlist("我", "要")) == ["咖啡"]


def test_uncovered_runs_are_reported_separately() -> None:
    assert uncovered("咖啡和牛奶", a_wordlist("和")) == ["咖啡", "牛奶"]


def test_punctuation_digits_and_latin_are_not_words() -> None:
    assert uncovered("我要 2 杯 coffee！", a_wordlist("我", "要", "杯")) == []


def test_an_empty_list_leaves_everything_uncovered() -> None:
    assert uncovered("我要水", a_wordlist()) == ["我要水"]

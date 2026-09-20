"""The word list and the segmentation that uses it (BACKLOG E4, rule 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.wordlist import (
    MissingWordlistError,
    Wordlist,
    expand_entry,
    expand_source,
    load_wordlist,
    pinyin_agrees,
    read_source,
    uncovered,
)


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


# ------------------------------------------------- the table's notation (E4/L-11)


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("爱", ("爱",)),
        ("爸爸|爸", ("爸爸", "爸")),  # alternative words
        ("白（形）", ("白",)),  # part of speech
        ("第（第二）", ("第",)),  # an example, not a form
        ("子（桌子）", ("子",)),
        ("多（形、代）", ("多",)),
        ("有（一）些", ("有些", "有一些")),  # an optional element inside the word
        ("好玩ㄦ", ("好玩", "好玩儿")),  # erhua, both spellings occur
        ("零|〇", ("零", "〇")),
    ],
)
def test_the_tables_notation_expands_to_the_forms_it_means(
    printed: str, expected: tuple[str, ...]
) -> None:
    assert expand_entry(printed) == expected


def test_a_form_written_twice_appears_once() -> None:
    assert expand_entry("好|好") == ("好",)


def test_the_expansion_keeps_table_order(tmp_path: Path) -> None:
    table = tmp_path / "hsk1.source.tsv"
    table.write_text("# source: test\n1\t爱\tài\n2\t爸爸|爸\tbàba|bà\n", encoding="utf-8")

    assert expand_source(read_source(table)) == ("爱", "爸爸", "爸")


def test_a_row_whose_pinyin_matches_its_characters_agrees() -> None:
    assert pinyin_agrees("爱好", "àihào")
    assert pinyin_agrees("帮忙", "bāng//máng")  # separable verb notation
    assert pinyin_agrees("好玩ㄦ", "hǎowánr")  # erhua
    assert pinyin_agrees("谁", "shéi / shuí")  # alternative readings


def test_neutral_tone_and_sandhi_spellings_agree() -> None:
    """The table writes 爸爸 as bàba and 不大 as bú dà; a dictionary does not."""
    assert pinyin_agrees("爸爸", "bàba")
    assert pinyin_agrees("不大", "bú dà")
    assert pinyin_agrees("一些", "yìxiē")


def test_a_misread_character_stops_agreeing() -> None:
    """The point of the check: 水 read as 永 would keep the pinyin shuǐ."""
    assert not pinyin_agrees("永", "shuǐ")
    assert not pinyin_agrees("爱好", "àihǎo hǎo")


def test_a_wrong_tone_stops_agreeing() -> None:
    """A mutation survived here: the check was blind to tone altogether.

    The syllable is right and only the mark is wrong, which is exactly the
    difference the neutral-tone allowance is for — so the allowance has to be
    narrow enough to still refuse this.
    """
    assert not pinyin_agrees("水", "shuí")
    assert not pinyin_agrees("爱好", "áihào")


def test_the_allowance_is_only_for_what_the_table_really_writes() -> None:
    assert pinyin_agrees("爸爸", "bàba")  # neutral tone, written unmarked
    assert not pinyin_agrees("爸爸", "bàbá")  # a mark that is simply wrong

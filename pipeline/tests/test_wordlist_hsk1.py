"""The HSK1 vocabulary, and the checks that stand behind its transcription.

The table was read off a scanned PDF with no text layer, so "is it right?" is
a real question and these are the answers that can be given mechanically:

  · the numbering runs 1-500 unbroken, which is what catches a dropped or
    duplicated row;
  · every row's characters agree with its own pinyin column, two independently
    read columns confirming each other, which is what catches a misread
    character;
  · the word list is exactly the expansion of the table, which is what stops
    a word entering the list that nobody transcribed.

None of that makes the transcription certainly correct. It makes the ways it
could be wrong narrow and named (docs/DECISIONS.md D-091).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from pipeline.seed_schema import load_seed_file
from pipeline.validate_seed import SEED_ROOT
from pipeline.wordlist import (
    WORDLIST_DIR,
    SourceEntry,
    expand_source,
    load_wordlist,
    order_breaks,
    pinyin_agrees,
    read_source,
    uncovered,
    wordlist_path,
)

SOURCE: Final = WORDLIST_DIR / "zh" / "hsk1.source.tsv"

#: PRD 5.1: level 1 of the standard is 500 words.
EXPECTED_ROWS: Final = 500


@pytest.fixture(scope="module")
def entries() -> list[SourceEntry]:
    return read_source(SOURCE)


def test_the_table_holds_every_numbered_row_and_no_others(entries: list[SourceEntry]) -> None:
    """A dropped row would be invisible in a list of words. A gap is not."""
    numbers = [entry.number for entry in entries]

    assert numbers == list(range(1, EXPECTED_ROWS + 1))


def test_every_row_agrees_with_its_own_pinyin(entries: list[SourceEntry]) -> None:
    """Two columns, read separately. A misread character stops matching its pinyin."""
    disagreeing = [
        f"{entry.number} {entry.word} {entry.reading}"
        for entry in entries
        if not pinyin_agrees(entry.word, entry.reading)
    ]

    assert disagreeing == []


def test_the_table_is_in_the_order_the_standard_prints_it(entries: list[SourceEntry]) -> None:
    """A third check, independent of the other two.

    The table is a dictionary: by syllable, then by tone, with homophones
    grouped under their head character. A misread syllable usually lands in
    the wrong place, and that is visible without knowing what the right
    answer was. All 500 rows are in order; the only backward steps are the
    16 places the table moves to the next homophone character (地 to 弟 to
    第, 坐下 to 做).
    """
    unexplained = [
        f"{before.number} {before.word} -> {after.number} {after.word}"
        for before, after in order_breaks(entries)
    ]

    assert unexplained == []


def test_a_row_transcribed_into_the_wrong_place_is_noticed(
    entries: list[SourceEntry],
) -> None:
    """The counterpart: the order check has to be able to fail."""
    misplaced = [*entries[:100], entries[400], *entries[100:]]

    assert order_breaks(misplaced) != []


def test_the_same_character_may_appear_twice_with_different_readings(
    entries: list[SourceEntry],
) -> None:
    """地 de and 地 dì are two rows, not a duplicated one (and so are 干, 还).

    Worth asserting rather than assuming: a duplicate check that reported
    those would have been switched off, and one that ignores the reading
    would not notice a genuinely repeated row.
    """
    pairs = [(entry.word, entry.reading) for entry in entries]
    repeated_characters = {
        entry.word for entry in entries if [e.word for e in entries].count(entry.word) > 1
    }

    assert len(set(pairs)) == len(pairs)
    assert repeated_characters == {"地", "干", "还"}


def test_the_word_list_is_exactly_the_expansion_of_the_table(entries: list[SourceEntry]) -> None:
    """`make wordlist` output, committed. A hand edit to the list fails here."""
    committed = [
        line.strip()
        for line in wordlist_path("zh", "HSK1").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert tuple(committed) == expand_source(entries)


def test_the_list_loads_with_the_provenance_r7_needs() -> None:
    wordlist = load_wordlist("zh", "HSK1")

    assert len(wordlist.words) > EXPECTED_ROWS  # variants expand it
    assert any("GF 0025-2021" in source for source in wordlist.sources)


@pytest.mark.parametrize(
    "word", ["我", "要", "水", "爸爸", "爸", "好玩儿", "好玩", "有一些", "有些"]
)
def test_words_and_their_variant_forms_are_all_in_the_list(word: str) -> None:
    assert word in load_wordlist("zh", "HSK1").words


@pytest.mark.parametrize("word", ["咖啡", "上海", "张", "第二"])
def test_words_the_table_does_not_list_are_not_in_it(word: str) -> None:
    """Including 第二 and 桌子's 子: the brackets there are examples, not forms."""
    assert word not in load_wordlist("zh", "HSK1").words


# --------------------------------------------- the two artefacts against each other
#
# The seed's example sentences come from the same document as this table — the
# grammar appendix of GF 0025-2021 — so nearly all of them should be inside the
# level-1 vocabulary. Running one against the other checks both at once, and it
# found a real defect in the segmentation (curly quotation marks were not
# treated as punctuation) before it was a test.

#: Sentences from the standard's own grammar appendix that use a word its own
#: level-1 table does not list. Not defects in either artefact: the appendix
#: writes 上海 where the word table has only 北京, and uses the measure word 张,
#: which is absent between 站 and 找. Worth knowing, because it means rule 1
#: will refuse sentences the standard itself prints (D-091).
KNOWN_OUT_OF_LEVEL: Final[dict[str, str]] = {
    "他要去上海，还要去北京。": "海",
    "房间里有两张桌子。": "张",
}


def test_the_seed_examples_are_inside_the_level_except_where_the_standard_is_not() -> None:
    seed = load_seed_file(SEED_ROOT / "zh-hsk3.0" / "hsk1.yaml")
    wordlist = load_wordlist("zh", "HSK1")

    outside = {
        sentence: uncovered(sentence, wordlist)
        for concept in seed.concepts
        for sentence in concept.example_sentences
        if uncovered(sentence, wordlist)
    }

    assert {sentence: missing[0] for sentence, missing in outside.items()} == KNOWN_OUT_OF_LEVEL


def test_the_check_would_notice_if_the_list_stopped_covering_the_seed() -> None:
    """The counterpart: the test above passing must mean something."""
    seed = load_seed_file(SEED_ROOT / "zh-hsk3.0" / "hsk1.yaml")
    sentences = [s for concept in seed.concepts for s in concept.example_sentences]
    empty = load_wordlist("zh", "HSK1").__class__(
        language="zh", level="HSK1", words=frozenset({"我"})
    )

    outside = [sentence for sentence in sentences if uncovered(sentence, empty)]

    assert len(outside) > len(KNOWN_OUT_OF_LEVEL)


def test_the_source_table_is_committed_beside_the_list() -> None:
    """The list is derived; without the table a correction has nowhere to go."""
    assert SOURCE.exists()
    assert Path(wordlist_path("zh", "HSK1")).exists()

"""Handing the catalogue to a translator and taking it back (bot/i18n_handoff.py).

The export is the easy half. What earns the tool its place is what it refuses
on the way back in: a translation that drops ``{resets_at}`` raises in front of
a learner in one language only, and the person most likely to drop it is a
translator who cannot read the code around it.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from bot import i18n, i18n_handoff
from bot.i18n_handoff import (
    COLUMNS,
    ENCODING,
    check_translations,
    parameters_of,
    pending,
    rows_for,
    write_csv,
)

KM = "សូមស្វាគមន៍"


@pytest.fixture
def catalogue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A three-locale catalogue of our own, so a test never edits the repo's."""
    locales = tmp_path / "locales"
    locales.mkdir()
    entries = {
        "en": {
            "bot.welcome": "Welcome.",
            "bot.quota_exhausted": "Allowance used up. It comes back at {resets_at}.",
            "bot.done": "Finished.",
        },
        "km": {
            "bot.welcome": "[[km:bot.welcome]]",
            "bot.quota_exhausted": "[[km:bot.quota_exhausted]]",
            "bot.done": f"{KM} ១",
        },
        "zh": {
            "bot.welcome": "[[zh:bot.welcome]]",
            "bot.quota_exhausted": "[[zh:bot.quota_exhausted]]",
            "bot.done": "[[zh:bot.done]]",
        },
    }
    for locale, data in entries.items():
        (locales / f"{locale}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
    monkeypatch.setattr(i18n, "LOCALES_DIR", locales)
    i18n.catalogue.cache_clear()
    yield locales
    i18n.catalogue.cache_clear()


def read(path: Path) -> list[dict[str, str]]:
    """The data rows, past the preamble — the same way a spreadsheet shows them."""
    with path.open(encoding=ENCODING, newline="") as handle:
        lines = [row for row in csv.reader(handle) if not (row and row[0].startswith("#"))]
    header, *body = lines
    return [dict(zip(header, row, strict=False)) for row in body]


# ------------------------------------------------------------------- export


def test_only_the_untranslated_strings_are_handed_over(catalogue: Path) -> None:
    keys = [row.key for row in rows_for("km")]

    assert keys == ["bot.quota_exhausted", "bot.welcome"]


def test_everything_can_be_handed_over_for_a_second_pass(catalogue: Path) -> None:
    """A reviewer reads the finished rows in context; a translator does not."""
    rows = {row.key: row for row in rows_for("km", everything=True)}

    assert set(rows) == {"bot.welcome", "bot.quota_exhausted", "bot.done"}
    assert rows["bot.done"].translation == f"{KM} ១"


def test_a_placeholder_is_never_offered_as_a_suggestion(catalogue: Path) -> None:
    """It would read as a draft to improve rather than as nothing at all."""
    rows = {row.key: row for row in rows_for("km", everything=True)}

    assert rows["bot.welcome"].translation == ""


def test_the_sheet_names_the_parameters_a_translation_must_keep(catalogue: Path) -> None:
    rows = {row.key: row for row in rows_for("km")}

    assert rows["bot.quota_exhausted"].parameters == "resets_at"
    assert rows["bot.welcome"].parameters == ""


def test_the_sheet_says_where_each_string_is_used(catalogue: Path) -> None:
    """A translator needs to know a button from a sentence, and cannot read Python."""
    sites = i18n_handoff.usage_sites()

    assert any(":" in where for where in sites.values())


def test_the_file_opens_as_utf8_in_a_spreadsheet(catalogue: Path, tmp_path: Path) -> None:
    path = tmp_path / "km.todo.csv"

    write_csv(rows_for("km"), path)

    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert list(read(path)[0]) == list(COLUMNS)


# ------------------------------------------------- the sheet explains itself
#
# The instructions used to be printed by `make i18n-export`, which reaches
# whoever ran the command and nobody after that: the file is emailed on, and
# the guidance is not in it.


def test_the_sheet_carries_its_own_instructions(catalogue: Path, tmp_path: Path) -> None:
    path = tmp_path / "km.todo.csv"

    write_csv(rows_for("km"), path, locale="km")
    text = path.read_text(encoding=ENCODING)

    assert "'translation'" in text
    assert "curly braces" in text  # the parameter rule, which is the dangerous one
    assert "'km'" in text


def test_the_notes_do_not_become_translations(catalogue: Path, tmp_path: Path) -> None:
    """They are rows in the same file; read back naively they are work."""
    path = tmp_path / "km.todo.csv"
    write_csv(rows_for("km"), path)

    assert all(not key.startswith("#") for key in i18n_handoff.read_csv(path))


def test_a_sorted_sheet_still_reads(catalogue: Path, tmp_path: Path) -> None:
    """A translator who sorts by any column scatters the notes into the data,
    so they are recognised by their marker and not by where they sit."""
    path = tmp_path / "km.todo.csv"
    write_csv(rows_for("km"), path)
    with path.open(encoding=ENCODING, newline="") as handle:
        lines = list(csv.reader(handle))
    header = next(row for row in lines if row and row[0] == "key")
    shuffled = [header, *sorted((r for r in lines if r is not header), key=lambda r: r[0])]
    with path.open("w", encoding=ENCODING, newline="") as handle:
        csv.writer(handle).writerows(shuffled)

    assert set(i18n_handoff.read_csv(path)) == {"bot.quota_exhausted", "bot.welcome"}


def test_a_sheet_whose_notes_were_deleted_still_reads(catalogue: Path, tmp_path: Path) -> None:
    """Some people tidy. The header is found, not assumed to be line one."""
    path = tmp_path / "km.todo.csv"
    write_csv(rows_for("km"), path)
    fill(path, {"bot.welcome": KM}, keep_notes=False)

    assert i18n_handoff.read_csv(path)["bot.welcome"] == KM


# -------------------------------------------------------------------- apply


def test_a_good_translation_is_accepted(catalogue: Path) -> None:
    good, problems = check_translations({"bot.welcome": KM}, locale="km")

    assert good == {"bot.welcome": KM}
    assert problems == []


def test_a_translation_that_drops_a_parameter_is_refused(catalogue: Path) -> None:
    """The failure this tool exists for: a KeyError in front of a learner."""
    _, problems = check_translations({"bot.quota_exhausted": KM}, locale="km")

    assert [p.key for p in problems] == ["bot.quota_exhausted"]
    assert "resets_at" in problems[0].message


def test_a_translation_that_keeps_its_parameter_is_accepted(catalogue: Path) -> None:
    good, problems = check_translations({"bot.quota_exhausted": f"{KM} {{resets_at}}"}, locale="km")

    assert problems == []
    assert "{resets_at}" in good["bot.quota_exhausted"]


def test_a_row_left_in_english_is_refused(catalogue: Path) -> None:
    """An untranslated row that is not a placeholder reads as finished."""
    _, problems = check_translations({"bot.welcome": "Welcome."}, locale="km")

    assert [p.key for p in problems] == ["bot.welcome"]
    assert "not written in km" in problems[0].message


def test_a_copied_placeholder_is_refused(catalogue: Path) -> None:
    _, problems = check_translations({"bot.welcome": "[[km:bot.welcome]]"}, locale="km")

    assert "copied instead of translated" in problems[0].message


def test_a_key_that_no_longer_exists_is_refused(catalogue: Path) -> None:
    _, problems = check_translations({"bot.gone": KM}, locale="km")

    assert "no such message key" in problems[0].message


def test_an_empty_row_is_simply_not_translated_yet(catalogue: Path) -> None:
    """Half a sheet coming back is normal and is not an error."""
    good, problems = check_translations({"bot.welcome": "   "}, locale="km")

    assert good == {}
    assert problems == []


def test_the_chinese_catalogue_is_checked_for_chinese(catalogue: Path) -> None:
    _, problems = check_translations({"bot.welcome": KM}, locale="zh")

    assert "not written in zh" in problems[0].message


# --------------------------------------------------------------- round trip


def fill(path: Path, translations: dict[str, str], *, keep_notes: bool = True) -> Path:
    """Fill the sheet in, the way a translator's spreadsheet saves it back."""
    with path.open(encoding=ENCODING, newline="") as handle:
        lines = list(csv.reader(handle))
    notes = [row for row in lines if row and row[0].startswith("#")]
    header = next(row for row in lines if row and row[0] == "key")
    body = lines[lines.index(header) + 1 :]
    with path.open("w", encoding=ENCODING, newline="") as handle:
        writer = csv.writer(handle)
        if keep_notes:
            writer.writerows(notes)
        writer.writerow(header)
        for row in body:
            if row and row[0].startswith("#"):
                continue
            cells = dict(zip(header, row, strict=False))
            cells["translation"] = translations.get(cells["key"], "")
            writer.writerow([cells.get(column, "") for column in header])
    return path


def test_a_filled_sheet_lands_in_the_catalogue(catalogue: Path, tmp_path: Path) -> None:
    sheet = tmp_path / "km.todo.csv"
    write_csv(rows_for("km"), sheet)
    fill(sheet, {"bot.welcome": KM, "bot.quota_exhausted": f"{KM} {{resets_at}}"})

    exit_code = i18n_handoff.main(["apply", str(sheet), "--locale", "km"])

    assert exit_code == 0
    assert pending("km") == []
    assert i18n.t("bot.welcome", locale="km") == KM


def test_a_sheet_with_a_broken_row_applies_the_rest_and_fails(
    catalogue: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Partial progress is kept; the exit code still says do not ship this."""
    sheet = tmp_path / "km.todo.csv"
    write_csv(rows_for("km"), sheet)
    fill(sheet, {"bot.welcome": KM, "bot.quota_exhausted": KM})  # second drops {resets_at}

    exit_code = i18n_handoff.main(["apply", str(sheet), "--locale", "km"])

    assert exit_code == 1
    assert "resets_at" in capsys.readouterr().err
    assert i18n.t("bot.welcome", locale="km") == KM
    assert pending("km") == ["bot.quota_exhausted"]


def test_something_that_is_not_the_exported_sheet_is_refused(
    catalogue: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "other.csv"
    other.write_text("a,b\n1,2\n", encoding=ENCODING)

    assert i18n_handoff.main(["apply", str(other)]) == 1
    assert "no 'key' column" in capsys.readouterr().err


# ------------------------------------------------- the repository as it stands


def test_the_real_catalogue_can_be_handed_over_today() -> None:
    """The reason this exists: 30 strings a native speaker can start on now,
    with no dependency on generation, TTS or anything else being ready."""
    assert len(rows_for("km")) == len(pending("km")) > 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("no parameters", ()),
        ("one {here}", ("here",)),
        ("{b} before {a}", ("a", "b")),
        ("{same} twice {same}", ("same",)),
    ],
)
def test_parameters_are_found_wherever_they_appear(text: str, expected: tuple[str, ...]) -> None:
    assert parameters_of(text) == expected

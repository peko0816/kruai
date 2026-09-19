"""The checks that keep the catalogue and the code in step (BACKLOG D7).

The first three tests are the gate: they run in `make check`, so a key that
exists in one locale and not another, a mistyped key, or a sentence written in
Python all fail a pull request.

The fourth is deliberately not a gate. Every translation is a placeholder
today, and a build that is red until a native speaker has been hired is a build
somebody switches off. The count is reported instead, and `make i18n-strict` —
the M2 launch gate — is what refuses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot import i18n, i18n_check


def parse(source: str, tmp_path: Path) -> Path:
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return path


# ------------------------------------------------------------------- the gate


def test_the_three_locales_carry_the_same_keys() -> None:
    findings = i18n_check.run()

    assert findings.missing_by_locale == {locale: set() for locale in i18n.SUPPORTED_LOCALES}
    assert findings.extra_by_locale == {locale: set() for locale in i18n.SUPPORTED_LOCALES}


def test_every_key_the_code_uses_exists() -> None:
    """A mistyped key is a MissingMessageError in front of a learner."""
    findings = i18n_check.run()

    assert findings.unknown_keys == []


def test_no_user_visible_string_is_written_in_python() -> None:
    """The D7 acceptance criterion, and CLAUDE.md section 7 made mechanical."""
    findings = i18n_check.run()

    assert findings.inline_strings == []


def test_the_repository_currently_passes_its_own_gate() -> None:
    findings = i18n_check.run()

    assert findings.blocking == []


# ------------------------------------- the checker, run against broken input
#
# Everything above asserts the repository is clean, which is worth knowing and
# proves nothing about the checker: `== []` passes just as well when the
# scanner has stopped looking. Four mutations survived on exactly that before
# these tests existed. Each one below builds something broken and insists it is
# found.


@pytest.fixture
def broken_tree(tmp_path: Path) -> Path:
    """A source root holding one file with both kinds of defect."""
    root = tmp_path / "src"
    root.mkdir()
    (root / "module.py").write_text(
        'Reply("bot.does_not_exist")\nmessage.reply_text("Copy written in Python")\n',
        encoding="utf-8",
    )
    return root


def catalogues(**by_locale: dict[str, str]) -> dict[str, dict[str, str]]:
    return dict(by_locale)


def test_a_key_missing_from_one_locale_is_found_and_blocks() -> None:
    findings = i18n_check.run(
        roots=(),
        catalogues=catalogues(
            km={"bot.welcome": "[[km:bot.welcome]]", "bot.help": "[[km:bot.help]]"},
            en={"bot.welcome": "Hello"},
        ),
    )

    assert findings.missing_by_locale["en"] == {"bot.help"}
    assert any("en.json is missing" in problem for problem in findings.blocking)


def test_a_key_only_one_locale_has_is_found_and_blocks() -> None:
    findings = i18n_check.run(
        roots=(),
        catalogues=catalogues(
            km={"bot.welcome": "[[km:bot.welcome]]"},
            en={"bot.welcome": "Hello", "bot.invented": "Surprise"},
        ),
    )

    assert findings.extra_by_locale["en"] == {"bot.invented"}
    assert any("unknown key" in problem for problem in findings.blocking)


def test_an_unknown_key_in_code_is_found_and_blocks(broken_tree: Path) -> None:
    findings = i18n_check.run(
        roots=(broken_tree,),
        catalogues=catalogues(km={"bot.welcome": "[[km:bot.welcome]]"}),
        base=broken_tree,
    )

    assert findings.unknown_keys == ["module.py:1 'bot.does_not_exist'"]
    assert any("unknown message key" in problem for problem in findings.blocking)


def test_copy_written_in_python_is_found_and_blocks(broken_tree: Path) -> None:
    findings = i18n_check.run(
        roots=(broken_tree,),
        catalogues=catalogues(km={"bot.does_not_exist": "[[km:x]]"}),
        base=broken_tree,
    )

    assert findings.inline_strings == ["module.py:2"]
    assert any("written in Python" in problem for problem in findings.blocking)


def test_a_broken_tree_fails_the_ordinary_run_not_only_the_strict_one(
    broken_tree: Path,
) -> None:
    """Structure is a per-PR gate; only the translation count waits for launch."""
    findings = i18n_check.run(
        roots=(broken_tree,),
        catalogues=catalogues(km={"bot.welcome": "Hello"}),
        base=broken_tree,
    )

    assert i18n_check.report(findings, strict=False) == 1


# ---------------------------------------------------- what counts as a key


def test_a_reply_key_is_found(tmp_path: Path) -> None:
    path = parse('Reply("bot.welcome")\n', tmp_path)

    assert i18n_check.keys_used_in(path) == [(1, "bot.welcome")]


def test_a_translated_call_is_found(tmp_path: Path) -> None:
    path = parse('i18n.t("bot.help", locale="km")\n', tmp_path)

    assert i18n_check.keys_used_in(path) == [(1, "bot.help")]


def test_a_constant_named_for_what_it_holds_is_found(tmp_path: Path) -> None:
    path = parse('AVATAR_DISCLAIMER_KEY = "avatar.ai_disclaimer"\n', tmp_path)

    assert i18n_check.keys_used_in(path) == [(1, "avatar.ai_disclaimer")]


def test_a_mapping_of_keys_is_found(tmp_path: Path) -> None:
    path = parse('PROMPT_KEYS = {"drill": "bot.prompt_drill", "qa": "bot.prompt_qa"}\n', tmp_path)

    assert sorted(key for _, key in i18n_check.keys_used_in(path)) == [
        "bot.prompt_drill",
        "bot.prompt_qa",
    ]


def test_a_log_event_is_not_a_message_key(tmp_path: Path) -> None:
    """``bot.turn`` and ``bot.welcome`` are the same shape and only one of them
    lives in locales/ (CODING_STANDARDS section 6 names log events that way)."""
    path = parse('log.info("bot.turn", step=1)\n', tmp_path)

    assert i18n_check.keys_used_in(path) == []


def test_an_error_code_is_not_a_message_key(tmp_path: Path) -> None:
    """AppError codes are the API's contract, not strings anybody reads."""
    path = parse('class Boom(AppError):\n    code = "attempt.audio_too_large"\n', tmp_path)

    assert i18n_check.keys_used_in(path) == []


def test_a_payload_field_is_not_a_message_key(tmp_path: Path) -> None:
    """The _KEY suffix means one thing; a JSONB field name is not it."""
    path = parse('TARGET_TEXT_FIELD = "target_text"\n', tmp_path)

    assert i18n_check.keys_used_in(path) == []


# ------------------------------------------- what counts as an inline string


@pytest.mark.parametrize(
    "source",
    [
        'message.reply_text("Try again")\n',
        'message.reply_voice(voice=url, caption="Say this")\n',
        'Outgoing(text="Well done")\n',
        'await message.reply_text("Lesson finished")\n',
    ],
)
def test_copy_written_in_python_is_caught(source: str, tmp_path: Path) -> None:
    path = parse(source, tmp_path)

    assert i18n_check.inline_strings_in(path) == [1]


@pytest.mark.parametrize(
    "source",
    [
        "message.reply_text(rendered)\n",
        "message.reply_voice(voice=url, caption=None)\n",
        'Outgoing(text="")\n',
        'message.reply_text(i18n.t("bot.welcome", locale=locale))\n',
        'message.reply_voice(voice="https://cdn.example/a.mp3")\n',
        "Outgoing(text=i18n.t(key, locale=locale))\n",
        'log.info("bot.turn", note="a long sentence that nobody reads")\n',
    ],
)
def test_a_rendered_string_is_not_a_violation(source: str, tmp_path: Path) -> None:
    """A URL is not copy, and a rendered key is the whole point."""
    path = parse(source, tmp_path)

    assert i18n_check.inline_strings_in(path) == []


# -------------------------------------------------------- the reported half


def test_the_placeholder_count_is_reported_rather_than_failed() -> None:
    """Every key is untranslated today. A gate that blocks unrelated work for
    weeks is a gate somebody removes."""
    findings = i18n_check.run()

    assert findings.placeholder_total > 0
    assert findings.blocking == []
    assert i18n_check.report(findings, strict=False) == 0


def test_the_launch_gate_refuses_while_translations_are_missing() -> None:
    findings = i18n_check.run()

    assert i18n_check.report(findings, strict=True) == 1


def test_english_is_the_one_locale_that_is_finished() -> None:
    findings = i18n_check.run()

    assert findings.placeholders_by_locale["en"] == set()
    assert findings.placeholders_by_locale["km"], (
        "km is placeholders until a native speaker writes it"
    )


def test_the_command_line_runs_both_ways() -> None:
    assert i18n_check.main([]) == 0
    assert i18n_check.main(["--strict"]) == 1


def test_the_checker_reads_both_the_bot_and_the_backend() -> None:
    """The API emits keys it never renders — the avatar disclaimer and the
    attempt feedback — so a typo there is just as invisible."""
    scanned = {path.name for path in i18n_check.python_files()}

    assert "handlers.py" in scanned
    assert "attempts.py" in scanned
    assert "resolve.py" in scanned

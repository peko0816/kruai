"""M0-2's blind listening test (m0/tts_blind.py).

What has to be true for this to produce an answer worth having is not that it
synthesises — any script does that — but that the listener cannot tell who
made which clip. Most of these tests are about the blinding, because a blind
test that leaks is worse than no test: it produces a number everyone trusts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from m0.tts_blind import (
    AXES,
    MIN_MEAN,
    MIN_REVIEWERS,
    Candidate,
    Clip,
    CorpusError,
    Passage,
    Verdict,
    as_markdown,
    load_corpus,
    main,
    shuffled,
    tally,
    write_key,
    write_sheet,
)

KM = "សួស្ដី តើអ្នកសុខសប្បាយទេ?"


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
    return Settings(_env_file=None, **{**base, **overrides})


def clip(passage_id: str, candidate: str) -> Clip:
    return Clip(
        passage=Passage(id=passage_id, text=KM, source="kh.moeys.oer"),
        candidate=Candidate.parse(candidate),
        resolved_voice=f"{candidate}-km-1",
        audio=b"ID3",
        audio_format="mp3",
        cost_usd_cents=1,
    )


def corpus(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "corpus.csv"
    path.write_text(body, encoding="utf-8")
    return path


# -------------------------------------------------------------------- blinding


def test_a_clip_name_says_nothing_about_who_made_it() -> None:
    """The one thing the whole exercise rests on."""
    name = clip("p01", "elevenlabs").name

    assert "eleven" not in name
    assert "p01" not in name
    assert name.endswith(".mp3")


def test_the_same_clip_gets_the_same_name_twice() -> None:
    """A reviewer sent the sheet again must not get different filenames, or
    their first round of scores cannot be matched to anything."""
    assert clip("p01", "azure").name == clip("p01", "azure").name


def test_different_vendors_of_one_passage_get_different_names() -> None:
    assert clip("p01", "azure").name != clip("p01", "google").name


def test_the_scoring_sheet_carries_no_vendor(tmp_path: Path) -> None:
    clips = [clip("p01", "azure"), clip("p01", "google")]
    path = tmp_path / "sheet.csv"

    write_sheet(clips, path)
    text = path.read_text(encoding="utf-8-sig")

    assert "azure" not in text
    assert "google" not in text
    assert "azure-km-1" not in text


def test_the_key_is_a_separate_file(tmp_path: Path) -> None:
    """Not a column nobody is supposed to read. A column gets read, and a
    reviewer who knows which is which is no longer independent."""
    clips = [clip("p01", "azure")]
    sheet, key = tmp_path / "sheet.csv", tmp_path / "key.csv"

    write_sheet(clips, sheet)
    write_key(clips, key)

    assert "azure" in key.read_text(encoding="utf-8-sig")
    assert "azure" not in sheet.read_text(encoding="utf-8-sig")


# ------------------------------------------------------------------- the order


def test_clips_of_one_passage_stay_together() -> None:
    """A reviewer compares three renderings of the same sentence; splitting
    them up removes the comparison the test exists to make."""
    clips = [clip(p, v) for p in ("p01", "p02") for v in ("a", "b", "c")]

    order = shuffled(clips, seed="x")

    assert len({c.passage.id for c in order[:3]}) == 1
    assert len({c.passage.id for c in order[3:]}) == 1


def test_the_vendor_order_is_not_the_same_in_every_group() -> None:
    """A fixed order stops being blind by the third passage: the listener
    notices the first clip is always the same voice and starts rating a
    vendor rather than a clip."""
    clips = [clip(f"p{n:02d}", v) for n in range(1, 9) for v in ("a", "b", "c")]

    order = shuffled(clips, seed="m0-2")
    per_passage = {
        p: tuple(c.candidate.label for c in order if c.passage.id == p)
        for p in {c.passage.id for c in clips}
    }

    assert len(set(per_passage.values())) > 1


def test_the_shuffle_is_reproducible() -> None:
    """Two people exporting the same material get the same sheet, so a score
    can be traced back to the clip it was given for."""
    clips = [clip(f"p{n}", v) for n in range(5) for v in ("a", "b", "c")]

    assert [c.name for c in shuffled(clips, seed="s")] == [
        c.name for c in shuffled(clips, seed="s")
    ]


def test_a_different_seed_gives_a_different_order() -> None:
    """So a second round can re-order the same clips."""
    clips = [clip(f"p{n}", v) for n in range(5) for v in ("a", "b", "c")]

    assert [c.name for c in shuffled(clips, seed="one")] != [
        c.name for c in shuffled(clips, seed="two")
    ]


# -------------------------------------------------------------------- the corpus


def test_a_well_formed_corpus_loads(tmp_path: Path) -> None:
    path = corpus(tmp_path, f"id,khmer,source\np01,{KM},kh.moeys.oer\n")

    assert load_corpus(path)[0].source == "kh.moeys.oer"


def test_a_passage_without_a_source_is_refused(tmp_path: Path) -> None:
    """R7: every piece of third-party text in this repository says where it
    came from, and a blind-test corpus is third-party text like any other."""
    path = corpus(tmp_path, f"id,khmer,source\np01,{KM},\n")

    with pytest.raises(CorpusError, match="source"):
        load_corpus(path)


def test_a_passage_that_is_not_khmer_is_refused(tmp_path: Path) -> None:
    """Synthesising English through a Khmer voice measures nothing."""
    path = corpus(tmp_path, "id,khmer,source\np01,Hello there,kh.moeys.oer\n")

    with pytest.raises(CorpusError, match="no Khmer script"):
        load_corpus(path)


def test_a_duplicate_id_is_refused(tmp_path: Path) -> None:
    """Two passages with one id collide in the clip names, and one set of
    scores silently lands on the other's clips."""
    path = corpus(tmp_path, f"id,khmer,source\np01,{KM},s\np01,{KM},s\n")

    with pytest.raises(CorpusError, match="twice"):
        load_corpus(path)


# ------------------------------------------------------------------- the tally


def sheet(
    marks: dict[str, tuple[int, int, str]], key: list[dict[str, str]]
) -> list[dict[str, str]]:
    return [
        {
            "clip": row["clip"],
            **dict(zip(AXES, map(str, marks[row["candidate"]][:2]), strict=True)),
            "fatal_error": marks[row["candidate"]][2],
        }
        for row in key
    ]


def a_key(candidates: tuple[str, ...]) -> list[dict[str, str]]:
    return [
        {
            "clip": clip("p01", c).name,
            "passage": "p01",
            "candidate": c,
            "provider": Candidate.parse(c).provider,
            "resolved_voice": "",
        }
        for c in candidates
    ]


def test_scores_are_attributed_to_the_right_vendor() -> None:
    key = a_key(("azure", "google"))
    marks = {"azure": (5, 5, ""), "google": (2, 2, "")}

    verdicts = {v.candidate: v for v in tally(key, [sheet(marks, key), sheet(marks, key)])}

    assert verdicts["azure"].means["naturalness"] == 5.0
    assert verdicts["google"].means["naturalness"] == 2.0


def test_one_reviewer_is_not_enough() -> None:
    """Two independent opinions is the acceptance, not a nicety: one person
    having a preference is not evidence about a language."""
    key = a_key(("azure",))
    marks = {"azure": (5, 5, "")}

    verdict = tally(key, [sheet(marks, key)])[0]

    assert verdict.reviewers < MIN_REVIEWERS
    assert not verdict.passed


def test_a_fatal_error_fails_a_vendor_with_good_averages() -> None:
    """A voice that mispronounces something badly is unusable however
    pleasant the rest of it sounds."""
    key = a_key(("azure",))
    marks = {"azure": (5, 5, "yes")}

    verdict = tally(key, [sheet(marks, key), sheet(marks, key)])[0]

    assert all(m >= MIN_MEAN for m in verdict.means.values())
    assert verdict.fatal > 0
    assert not verdict.passed


def test_both_axes_have_to_clear_the_bar() -> None:
    """Clear and robotic is a different fault from natural and mushy, and
    averaging them together would let either one through."""
    key = a_key(("azure",))
    marks = {"azure": (5, 2, "")}

    verdict = tally(key, [sheet(marks, key), sheet(marks, key)])[0]

    assert not verdict.passed


def test_a_blank_score_is_skipped_rather_than_counted_as_zero() -> None:
    """A half-filled sheet is normal; counting the gaps as zeroes would fail
    every vendor on a sheet somebody did not finish."""
    key = a_key(("azure",))
    rows = [{"clip": key[0]["clip"], "naturalness": "5", "intelligibility": "", "fatal_error": ""}]

    verdict = tally(key, [rows, rows])[0]

    assert verdict.means["naturalness"] == 5.0


# ------------------------------------------------------------------ the report


def test_the_report_names_the_winner() -> None:
    text = as_markdown(
        [
            Verdict("azure", dict.fromkeys(AXES, 4.5), fatal=0, reviewers=2),
            Verdict("google", dict.fromkeys(AXES, 3.0), fatal=0, reviewers=2),
        ]
    )

    assert "Choose `azure`" in text


def test_the_report_says_so_when_nothing_passes() -> None:
    """And says that the way out is a product decision, not a technical one."""
    text = as_markdown([Verdict("azure", dict.fromkeys(AXES, 2.0), fatal=0, reviewers=2)])

    assert "No candidate passes" in text
    assert "product decision" in text


def test_the_report_leaves_the_voice_mapping_to_be_filled() -> None:
    """E8 reads that table; without it TTS_PROVIDER has nothing to resolve a
    logical voice to."""
    text = as_markdown([Verdict("azure", dict.fromkeys(AXES, 4.5), fatal=0, reviewers=2)])

    assert "km_narrator" in text
    assert "E8" in text


# --------------------------------------------------------------------- the CLI


def test_a_billed_vendor_needs_the_spend_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three vendors over fifteen passages is three bills, not one."""
    import m0.tts_blind as module

    monkeypatch.setattr(module, "get_settings", settings)
    path = corpus(tmp_path, f"id,khmer,source\np01,{KM},kh.moeys.oer\n")

    assert main(["export", str(path), "--candidates", "azure:a,azure:b"]) == 2
    assert "bill per character" in capsys.readouterr().err


def test_the_fake_provider_runs_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import m0.tts_blind as module

    monkeypatch.setattr(module, "get_settings", settings)
    path = corpus(tmp_path, f"id,khmer,source\np01,{KM},kh.moeys.oer\n")

    code = main(
        ["export", str(path), "--out", str(tmp_path / "clips"), "--key", str(tmp_path / "k.csv")]
    )

    assert code == 0
    assert (tmp_path / "clips" / "scoring-sheet.csv").is_file()
    assert (tmp_path / "k.csv").is_file()


def test_scoring_refuses_a_single_sheet(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key = tmp_path / "key.csv"
    write_key([clip("p01", "azure")], key)
    one = tmp_path / "one.csv"
    one.write_text("clip,naturalness,intelligibility,fatal_error\nx,5,5,\n", encoding="utf-8")

    assert main(["score", str(one), "--key", str(key), "--out", str(tmp_path / "d.md")]) == 2
    assert "independent reviewers" in capsys.readouterr().err


def test_the_clips_and_the_key_are_written_to_different_places(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So sending the folder cannot send the key with it."""
    import m0.tts_blind as module

    monkeypatch.setattr(module, "get_settings", settings)
    path = corpus(tmp_path, f"id,khmer,source\np01,{KM},kh.moeys.oer\n")
    out, key = tmp_path / "clips", tmp_path / "key.csv"

    main(["export", str(path), "--out", str(out), "--key", str(key)])

    assert key.parent != out
    assert not any(p.name == key.name for p in out.iterdir())


# ------------------------------------- comparing voices of one vendor (D-125)
#
# Of the three vendors planned, only Azure has Khmer. The question became which
# of its voices, and a candidate is a provider pinned to one of them.


def test_a_candidate_parses_a_pinned_voice() -> None:
    c = Candidate.parse("azure:km-KH-SreymomNeural")

    assert (c.provider, c.voice) == ("azure", "km-KH-SreymomNeural")
    assert c.label == "azure:km-KH-SreymomNeural"


def test_a_bare_provider_is_still_a_candidate() -> None:
    """Comparing vendors still works the way it did."""
    assert Candidate.parse("fake").label == "fake"


def test_two_voices_of_one_vendor_get_different_clip_names() -> None:
    """Named by provider alone, the second voice would overwrite the first."""
    assert (
        clip("p01", "azure:km-KH-SreymomNeural").name
        != clip("p01", "azure:km-KH-PisethNeural").name
    )


def test_the_pinned_voice_reaches_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the registry every runtime caller uses, so the test hears what a
    pack build would produce rather than a voice reached some other way."""
    import asyncio

    from app.services.tts import registry
    from app.services.tts.base import Voice
    from app.services.tts.fake import FakeTTS
    from m0.tts_blind import synthesise

    built: list[FakeTTS] = []

    def spy(**kwargs: Any) -> FakeTTS:
        provider = FakeTTS(**kwargs)
        built.append(provider)
        return provider

    monkeypatch.setitem(registry._FACTORIES, "spy", spy)
    passages = [Passage(id="p01", text=KM, source="corpus.tatoeba")]

    asyncio.run(
        synthesise(
            passages,
            candidates=[Candidate.parse("spy:km-KH-PisethNeural")],
            settings=settings(),
        )
    )

    assert built[0].requested_voices == {Voice.KM_NARRATOR: "km-KH-PisethNeural"}


def test_the_fake_does_not_claim_a_vendor_voice() -> None:
    """ARCHITECTURE 3.2: a vendor voice name on silence would survive into a
    pack that looks real. The fake accepts the mapping and still says fake."""
    import asyncio

    from app.services.tts.base import Voice
    from app.services.tts.fake import FakeTTS

    tts = FakeTTS(voices={Voice.KM_NARRATOR: "km-KH-SreymomNeural"})
    result = asyncio.run(tts.synthesize(KM, voice=Voice.KM_NARRATOR))

    assert result.resolved_voice == "fake"


def test_the_winning_voice_fills_the_narrator_mapping() -> None:
    """E8 reads that table; the decision should arrive already written into it."""
    text = as_markdown(
        [
            Verdict("azure:km-KH-SreymomNeural", dict.fromkeys(AXES, 4.6), fatal=0, reviewers=2),
            Verdict("azure:km-KH-PisethNeural", dict.fromkeys(AXES, 4.1), fatal=0, reviewers=2),
        ]
    )

    assert "| `km_narrator` | `km-KH-SreymomNeural` |" in text


def test_two_billed_voices_are_still_one_spend_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two voices of one vendor is two runs over the corpus, so it is billed —
    but the guard names the vendor once rather than once per voice."""
    import m0.tts_blind as module

    monkeypatch.setattr(module, "get_settings", settings)
    path = corpus(tmp_path, f"id,khmer,source\np01,{KM},corpus.tatoeba\n")

    assert main(["export", str(path), "--candidates", "azure:a,azure:b"]) == 2
    assert "['azure']" in capsys.readouterr().err

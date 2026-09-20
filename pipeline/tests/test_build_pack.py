"""Freezing a level into a pack (BACKLOG E6).

The acceptance criterion is that FakeTTS produces a complete pack, which is the
first test. The rest are about the things a pack is for: that every fixed
string has audio, that the audio can be told apart from the audio of a text
that has since changed, and that the build refuses to freeze content nothing
checked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services.tts.base import SynthesisResult, TTSProvider, Voice
from pipeline import build_pack as build_module
from pipeline.build_pack import Synthesiser, build
from pipeline.draft import Draft
from pipeline.pack import Pack, text_hash

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
        # One of each is enough to exercise the shape; the real minimums are
        # validate.py's business and are tested there.
        "pipeline_min_sentences_per_concept": 1,
        "pipeline_min_substitutions_per_concept": 1,
        "pipeline_min_dialogues_per_concept": 1,
    }
    return Settings(**{**base, **overrides})


#: Distinct per concept, because validate.py refuses near-duplicate target
#: sentences and it is right to — two concepts drilling the same sentence is a
#: wasted slot (D-088). All HSK1 vocabulary.
_NOUNS = ("水", "茶", "书", "饭", "车")
_PINYIN = ("shuǐ", "chá", "shū", "fàn", "chē")


def a_draft(*, concepts: int = 2, sentence: str | None = None) -> Draft:
    """A draft that passes validate.py: real HSK1 words, real Khmer."""
    return Draft.model_validate(
        {
            "language": "zh",
            "level": "HSK1",
            "licence": "public standards",
            "sources": [{"id": "zh.standard.2021"}],
            "seed_file": "test.yaml",
            "provider": "fake",
            "model": "fake",
            "generated_at": "2026-09-20T00:00:00Z",
            "usage": {},
            "concepts": [
                {
                    "slug": f"zh.hsk1.c{number}",
                    "pattern": "我要 + [名词]",
                    "hskk_task_types": ["listen_and_repeat"],
                    "km_explanation": f"{KM}{'។' * (number + 1)}",
                    "target_sentences": [
                        {
                            "zh": sentence or f"我要{_NOUNS[number]}。",
                            "pinyin": f"wǒ yào {_PINYIN[number]}",
                            "km_gloss": KM,
                        }
                    ],
                    # Shared on purpose: a substitution repeated across concepts
                    # is normal, and it is what the content-addressing tests
                    # need. Dedup does not compare substitutions.
                    "substitutions": [{"zh": "水", "pinyin": "shuǐ", "km_gloss": KM}],
                    "dialogues": [
                        {
                            "hskk_task_type": "listen_and_repeat",
                            "prompt_zh": f"你要{_NOUNS[number]}吗",
                            "prompt_km": KM,
                            "sample_answer_zh": f"我要{_NOUNS[number]}",
                        }
                    ],
                }
                for number in range(concepts)
            ],
        }
    )


def make(draft: Draft, tmp_path: Path, **kwargs: Any) -> Pack:
    return asyncio.run(
        build(
            draft,
            settings=kwargs.pop("settings", settings()),
            media_dir=tmp_path / "media",
            version=kwargs.pop("version", "v1"),
            validated=kwargs.pop("validated", True),
            **kwargs,
        )
    )


# ----------------------------------------------------------- the acceptance


def test_the_fake_provider_produces_a_complete_pack(tmp_path: Path) -> None:
    """E6's acceptance criterion: every fixed string has audio behind it."""
    pack = make(a_draft(), tmp_path)

    assert pack.missing_audio == ()
    assert len(pack.concepts) == 2
    assert pack.media


def test_every_clip_is_on_disk_where_the_pack_says_it_is(tmp_path: Path) -> None:
    pack = make(a_draft(), tmp_path)

    for asset in pack.media:
        written = tmp_path / "media" / asset.path
        assert written.exists()
        assert written.stat().st_size == asset.bytes
        assert asset.duration_ms > 0


def test_the_pack_round_trips_through_json(tmp_path: Path) -> None:
    """import_pack.py reads this file back; the shape is the contract."""
    pack = make(a_draft(), tmp_path)

    reloaded = Pack.model_validate_json(pack.model_dump_json())

    assert reloaded == pack
    assert reloaded.checksum_matches


# ------------------------------------------------------- content addressing


def test_the_same_text_is_synthesised_once(tmp_path: Path) -> None:
    """A level repeats itself; on a billed provider that repetition is money."""
    pack = make(a_draft(concepts=3), tmp_path)

    # Three concepts share one sentence, one substitution and one Khmer gloss,
    # and differ in the explanation and the dialogue prompt.
    assert pack.usage.reused > 0
    assert len(pack.media) == pack.usage.tts_calls


def test_audio_is_named_after_the_text_that_produced_it(tmp_path: Path) -> None:
    pack = make(a_draft(), tmp_path)
    spoken = {asset.text: asset for asset in pack.media}

    asset = spoken["我要水。"]

    assert asset.id in asset.path
    assert asset.id == text_hash("我要水。", voice="zh_model", speaking_rate=asset.speaking_rate)


def test_a_changed_text_gets_a_different_identity() -> None:
    """DATA_MODEL's source_text_hash: media goes stale when the text moves."""
    before = text_hash("我要水。", voice="zh_model", speaking_rate=0.9)
    after = text_hash("我要茶。", voice="zh_model", speaking_rate=0.9)

    assert before != after


def test_the_voice_and_rate_are_part_of_the_identity() -> None:
    """The same sentence read at 0.9 and at 1.0 are different files."""
    slow = text_hash("我要水。", voice="zh_model", speaking_rate=0.9)
    quick = text_hash("我要水。", voice="zh_model", speaking_rate=1.0)
    khmer = text_hash("我要水。", voice="km_narrator", speaking_rate=0.9)

    assert len({slow, quick, khmer}) == 3


def test_chinese_is_read_slowly_and_khmer_is_not(tmp_path: Path) -> None:
    """PRD 6.3: the Chinese demonstration is 0.9x. The rates come from config."""
    pack = make(a_draft(), tmp_path)
    by_voice = {asset.voice: asset for asset in pack.media}

    assert by_voice["zh_model"].speaking_rate == 0.9
    assert by_voice["km_narrator"].speaking_rate == 1.0


# ------------------------------------------------------------------ checksum


def test_a_pack_carries_a_checksum_of_its_own_contents(tmp_path: Path) -> None:
    pack = make(a_draft(), tmp_path)

    assert pack.checksum
    assert pack.checksum_matches


def test_editing_a_pack_breaks_its_checksum(tmp_path: Path) -> None:
    """content_packs.checksum is how an altered pack is caught at import."""
    pack = make(a_draft(), tmp_path)

    tampered = pack.model_copy(update={"licence": "something else"})

    assert not tampered.checksum_matches


# ------------------------------------------------------------------ failures


class _SilentFailure(TTSProvider):
    """A provider that fails one clip, the way base.py says failures arrive."""

    name = "failing"

    def __init__(self) -> None:
        self.seen = 0

    async def synthesize(self, text: str, **kwargs: Any) -> SynthesisResult:
        self.seen += 1
        if self.seen == 1:
            return SynthesisResult(ok=False, error_code="tts.timeout")
        return SynthesisResult(
            ok=True, audio=b"\xff\xfbT\xc4" + bytes(188), duration_ms=24, resolved_voice="x"
        )

    def available_voices(self) -> dict[Voice, str]:
        return dict.fromkeys(Voice, "x")


async def test_a_failed_clip_is_recorded_rather_than_dropped(tmp_path: Path) -> None:
    synth = Synthesiser(settings=settings(), media_dir=tmp_path / "media")
    provider = _SilentFailure()

    first = await synth.say("我要水。", language="zh", provider=provider)
    second = await synth.say("我要茶。", language="zh", provider=provider)

    assert first == ""  # the failure
    assert second != ""
    assert synth.failures == [("我要水。", "tts.timeout")]


async def test_blank_text_is_not_sent_to_a_provider(tmp_path: Path) -> None:
    synth = Synthesiser(settings=settings(), media_dir=tmp_path / "media")
    provider = _SilentFailure()

    assert await synth.say("   ", language="zh", provider=provider) == ""
    assert provider.seen == 0


# ----------------------------------------------------------------------- CLI


def write_draft(tmp_path: Path, draft: Draft) -> Path:
    path = tmp_path / "zh-HSK1.draft.json"
    path.write_text(draft.model_dump_json(), encoding="utf-8")
    return path


def test_video_is_refused_because_the_content_is_not_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """PRD 14 and BACKLOG E6: the switch exists, the branch does not."""
    monkeypatch.setattr(build_module, "get_settings", settings)
    draft = write_draft(tmp_path, a_draft())

    exit_code = build_module.main([str(draft), "--with-video"])

    assert exit_code == 2
    assert "PRD section 14" in capsys.readouterr().err


def test_the_configuration_switch_refuses_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """PIPELINE_WITH_VIDEO=true must not be a way round the flag."""
    monkeypatch.setattr(build_module, "get_settings", lambda: settings(pipeline_with_video=True))
    draft = write_draft(tmp_path, a_draft())

    assert build_module.main([str(draft)]) == 2
    assert "not implemented" in capsys.readouterr().err


def test_a_billed_provider_will_not_run_without_being_told_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(build_module, "get_settings", lambda: settings(tts_provider="azure"))
    draft = write_draft(tmp_path, a_draft())

    assert build_module.main([str(draft)]) == 2
    assert "--confirm-spend" in capsys.readouterr().err


def test_an_invalid_draft_is_not_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(build_module, "get_settings", settings)
    draft = write_draft(tmp_path, a_draft(sentence="我要咖啡。"))  # 咖啡 is above HSK1

    exit_code = build_module.main([str(draft), "--out", str(tmp_path / "pack.json")])

    assert exit_code == 1
    assert "--allow-invalid" in capsys.readouterr().err
    assert not (tmp_path / "pack.json").exists()


def test_forcing_the_build_marks_the_pack_unvalidated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D-094: the override has to outlive the shell it was typed in."""
    monkeypatch.setattr(build_module, "get_settings", settings)
    draft = write_draft(tmp_path, a_draft(sentence="我要咖啡。"))
    out = tmp_path / "pack.json"

    exit_code = build_module.main([str(draft), "--allow-invalid", "--out", str(out)])

    assert exit_code == 0
    assert Pack.model_validate_json(out.read_text(encoding="utf-8")).validated is False
    assert "import_pack will refuse it" in capsys.readouterr().out


def test_a_level_with_no_word_list_is_refused_rather_than_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unvalidatable is not the same as valid, and must not build silently."""
    monkeypatch.setattr(build_module, "get_settings", settings)
    monkeypatch.setattr("pipeline.wordlist.WORDLIST_DIR", tmp_path / "empty")
    draft = write_draft(tmp_path, a_draft())

    assert build_module.main([str(draft)]) == 2
    assert "cannot judge this draft" in capsys.readouterr().err


def test_a_clean_build_reports_what_it_made(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(build_module, "get_settings", settings)
    draft = write_draft(tmp_path, a_draft())
    out = tmp_path / "pack.json"

    exit_code = build_module.main([str(draft), "--out", str(out)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "2 concept(s)" in captured.out
    assert "warning" not in captured.out
    assert Pack.model_validate_json(out.read_text(encoding="utf-8")).validated is True

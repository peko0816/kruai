"""The self-check has to fail on a bad deployment and name what is missing.

BACKLOG B5's acceptance is exactly that: misconfigure it on purpose, and the
error should say which language is uncovered. So these tests care about the
message as much as the exception — an error that says "configuration invalid"
sends someone reading source at 3am.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.services.provider_errors import ProviderConfigurationError
from app.services.scoring.base import (
    AssessMode,
    Language,
    PronunciationResult,
    PronunciationScorer,
)
from app.services.selfcheck import (
    REQUIRED_LANGUAGES,
    REQUIRED_VOICES,
    CapabilityProblem,
    check_provider_configuration,
    collect_problems,
)
from app.services.tts.base import SynthesisResult, TTSProvider, Voice

_MINIMAL: dict[str, str] = {
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "TELEGRAM_BOT_TOKEN": "",
    "AZURE_SPEECH_KEY": "",
    "AZURE_SPEECH_REGION": "",
    "GOOGLE_APPLICATION_CREDENTIALS": "",
    "ELEVENLABS_API_KEY": "",
    "OPENAI_API_KEY": "",
    "PAYWAY_MERCHANT_ID": "",
    "PAYWAY_API_KEY": "",
    "PAYWAY_BASE_URL": "",
    "BAKONG_TOKEN": "",
    "JWT_SECRET": "",
}

#: 32 characters: Settings refuses anything shorter when ENV=prod
#: (RFC 7518 section 3.2), and several tests below build a production config.
PROD_JWT_SECRET = "a-production-length-signing-key-0"


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def settings(**overrides: str) -> Settings:
    values: dict[str, Any] = {**_MINIMAL, **overrides}
    return Settings(_env_file=None, **values)


class EnglishOnlyScorer(PronunciationScorer):
    """A vendor with no Chinese — the gap B5 exists to catch."""

    name = "en-only"

    def supports(self, language: Language) -> bool:
        return language is Language.EN_US

    async def assess(
        self,
        audio: bytes,
        *,
        language: Language,
        mode: AssessMode,
        reference_text: str | None = None,
        audio_format: str = "wav",
    ) -> PronunciationResult:
        raise AssertionError("the self-check never assesses")


class NoKhmerTTS(TTSProvider):
    """The shape M0-2 is worried about: no km-KH voice."""

    name = "no-khmer"

    def available_voices(self) -> dict[Voice, str]:
        return {Voice.ZH_MODEL: "vendor-zh-1"}

    async def synthesize(
        self,
        text: str,
        *,
        voice: Voice,
        speaking_rate: float = 1.0,
        audio_format: str = "mp3",
    ) -> SynthesisResult:
        raise AssertionError("the self-check never synthesises")


@pytest.fixture
def broken_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.scoring import registry as scoring_registry
    from app.services.tts import registry as tts_registry

    monkeypatch.setitem(scoring_registry._FACTORIES, "en-only", EnglishOnlyScorer)
    monkeypatch.setitem(tts_registry._FACTORIES, "no-khmer", NoKhmerTTS)


# ------------------------------------------------------------- the happy path


def test_the_shipped_default_configuration_is_coherent() -> None:
    """Gate G-B in one assertion: all-fake has to be a working deployment."""
    assert collect_problems(settings()) == []
    check_provider_configuration(settings())


def test_multiple_payment_channels_stay_coherent() -> None:
    configured = settings(PAYMENT_PROVIDERS="fake,fake_manual", SUPPORTED_CURRENCIES="USD,KHR")
    assert collect_problems(configured) == []


# ------------------------------------------------------ the acceptance scenario


@pytest.mark.usefixtures("broken_providers")
def test_a_scorer_without_chinese_fails_the_boot_and_names_the_language() -> None:
    """BACKLOG B5 acceptance, word for word."""
    with pytest.raises(ProviderConfigurationError) as exc:
        check_provider_configuration(settings(SCORING_PROVIDER="en-only"))

    message = str(exc.value)
    assert "zh-CN" in message
    assert "scoring" in message


@pytest.mark.usefixtures("broken_providers")
def test_a_tts_without_khmer_names_the_missing_voices() -> None:
    problems = collect_problems(settings(TTS_PROVIDER="no-khmer"))
    missing = {problem.requirement for problem in problems}

    assert missing == {"km_narrator", "km_feedback"}
    assert all(problem.adapter == "tts" for problem in problems)


def test_a_misspelled_provider_name_is_caught_at_boot() -> None:
    with pytest.raises(ProviderConfigurationError, match="unknown scoring provider 'azur'"):
        check_provider_configuration(settings(SCORING_PROVIDER="azur"))


# ------------------------------------------------- everything at once, not one


@pytest.mark.usefixtures("broken_providers")
def test_every_problem_is_reported_in_one_pass() -> None:
    """Fixing one and rerunning to find the next costs a deploy round each time."""
    problems = collect_problems(
        settings(SCORING_PROVIDER="en-only", TTS_PROVIDER="no-khmer", LLM_PROVIDER="nope")
    )
    assert {problem.adapter for problem in problems} == {"scoring", "tts", "llm"}


@pytest.mark.usefixtures("broken_providers")
def test_the_raised_message_lists_them_all() -> None:
    with pytest.raises(ProviderConfigurationError) as exc:
        check_provider_configuration(settings(SCORING_PROVIDER="en-only", TTS_PROVIDER="no-khmer"))

    message = str(exc.value)
    assert "3 provider capability problem(s)" in message
    assert message.count("  - ") == 3


# -------------------------------------------------------------------- payments


def test_no_enabled_channel_is_a_problem() -> None:
    problems = collect_problems(settings(PAYMENT_PROVIDERS=""))
    assert [problem.adapter for problem in problems] == ["payments"]
    assert "nothing can take money" in problems[0].detail


def test_a_currency_no_channel_settles_is_caught() -> None:
    """fake_manual takes USD only, so enabling KHR alongside it is incoherent."""
    problems = collect_problems(
        settings(PAYMENT_PROVIDERS="fake_manual", SUPPORTED_CURRENCIES="USD,KHR")
    )
    assert [problem.requirement for problem in problems] == ["KHR"]


def test_a_bad_channel_name_suppresses_the_currency_report() -> None:
    """Reporting KHR as uncovered while a channel is unbuildable points at the
    wrong fix; the name is the cause."""
    problems = collect_problems(settings(PAYMENT_PROVIDERS="typo", SUPPORTED_CURRENCIES="USD,KHR"))
    assert [problem.requirement for problem in problems] == ["typo"]


def test_a_test_provider_in_production_fails_the_boot() -> None:
    """D-010's guard reached through the self-check, which is where a deploy
    pipeline would hit it."""
    production = settings(ENV="prod", JWT_SECRET=PROD_JWT_SECRET, TELEGRAM_BOT_TOKEN="bot")
    with pytest.raises(ProviderConfigurationError, match="published in its own source"):
        check_provider_configuration(production)


# ------------------------------------------------------------- what is required


def test_v1_requires_chinese_only() -> None:
    """PRD 1.3. English shares the schema but has no content packs yet, so
    demanding a scorer for it would fail every working deployment."""
    assert REQUIRED_LANGUAGES == (Language.ZH_CN,)


def test_v1_requires_both_khmer_voices_and_the_chinese_model() -> None:
    """Khmer is the teaching language; Chinese is what learners imitate."""
    assert set(REQUIRED_VOICES) == {Voice.KM_NARRATOR, Voice.KM_FEEDBACK, Voice.ZH_MODEL}
    assert Voice.EN_MODEL not in REQUIRED_VOICES


# --------------------------------------------------------------- the reporting


def test_a_problem_reads_as_a_sentence() -> None:
    problem = CapabilityProblem("scoring", "zh-CN", "no provider supports it")
    assert str(problem) == "scoring: cannot serve zh-CN — no provider supports it"


def test_a_genuine_bug_in_a_factory_is_not_reported_as_misconfiguration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catching ValueError broadly would send someone to the wrong file."""
    from app.services.scoring import registry as scoring_registry

    def exploding() -> PronunciationScorer:
        raise RuntimeError("a real bug inside the factory")

    monkeypatch.setitem(scoring_registry._FACTORIES, "fake", exploding)

    with pytest.raises(RuntimeError, match="a real bug inside the factory"):
        collect_problems(settings())

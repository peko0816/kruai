"""Settings must stay in lockstep with docs/CONFIG_REFERENCE.md and .env.example.

That correspondence is the A3 acceptance criterion, so it is asserted here
rather than eyeballed: three files drift apart silently otherwise, and the
symptom (a threshold that quietly keeps its old value) is invisible in review.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Plan, Settings

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_REFERENCE = _REPO_ROOT / "docs" / "CONFIG_REFERENCE.md"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

# Values that make a Settings() valid without touching any file.
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
    """Strip every Settings key from the real environment.

    Without this a developer who exports ENV or DATABASE_URL in their shell
    gets different test results than CI does.
    """
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def build(**overrides: str) -> Settings:
    """Settings from an explicit mapping, with no .env file involved."""
    values: dict[str, Any] = {**_MINIMAL, **overrides}
    return Settings(_env_file=None, **values)


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def reference_keys() -> set[str]:
    """Config keys documented in CONFIG_REFERENCE.md.

    Two shapes appear there: table rows for regular settings, and a fenced
    block of ``KEY=`` lines for the secrets section.
    """
    text = _CONFIG_REFERENCE.read_text(encoding="utf-8")
    table = set(re.findall(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|", text, re.M))
    block = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M))
    return table | block


def env_example_keys() -> set[str]:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M))


# --------------------------------------------------------------- correspondence


def test_settings_fields_match_config_reference() -> None:
    documented = reference_keys()
    declared = {name.upper() for name in Settings.model_fields}

    assert declared - documented == set(), (
        "Settings declares undocumented keys. Add them to "
        "docs/CONFIG_REFERENCE.md (see its '新增配置项的流程')."
    )
    assert documented - declared == set(), (
        "CONFIG_REFERENCE documents keys Settings does not declare."
    )


def test_env_example_matches_config_reference() -> None:
    assert env_example_keys() == reference_keys()


def test_env_example_produces_valid_settings() -> None:
    """The template we tell people to copy must actually work."""
    settings = Settings(_env_file=_ENV_EXAMPLE)

    assert settings.env == "dev"
    # Gate G-B depends on this being the shipped default.
    assert settings.scoring_provider == "fake"
    assert settings.tts_provider == "fake"
    assert settings.llm_provider == "fake"
    assert settings.payment_providers == ("fake",)
    assert settings.cost_ledger_required is True


def test_env_example_values_equal_code_defaults() -> None:
    """A default changed in code but not in the template is a silent trap."""
    from_template = Settings(_env_file=_ENV_EXAMPLE)
    mismatched: list[str] = []

    for name, field in Settings.model_fields.items():
        if field.is_required():
            continue
        if getattr(from_template, name) != field.get_default():
            mismatched.append(name.upper())

    assert mismatched == [], f".env.example disagrees with Settings defaults: {mismatched}"


# ------------------------------------------------------------------ extra="forbid"


def test_unknown_key_in_env_file_fails_startup(tmp_path: Path) -> None:
    """The A3 acceptance criterion: one stray key and the process must not start."""
    body = "\n".join(f"{k}={v}" for k, v in _MINIMAL.items())
    env = write_env(tmp_path, body + "\nSCORING_PASS_THRESHOLDD=75\n")

    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=env)

    assert "scoring_pass_thresholdd" in str(exc.value).lower()


def test_missing_required_key_fails_startup(tmp_path: Path) -> None:
    body = "\n".join(f"{k}={v}" for k, v in _MINIMAL.items() if k != "DATABASE_URL")
    env = write_env(tmp_path, body)

    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=env)

    assert "database_url" in str(exc.value).lower()


# ---------------------------------------------------------------- csv parsing


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("azure", ("azure",)),
        ("azure,iflytek", ("azure", "iflytek")),
        (" azure , iflytek ", ("azure", "iflytek")),
        ("azure,,iflytek", ("azure", "iflytek")),
    ],
)
def test_csv_provider_list(raw: str, expected: tuple[str, ...]) -> None:
    assert build(SCORING_FALLBACK_PROVIDERS=raw).scoring_fallback_providers == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1,3", (1, 3)), ("1", (1,)), ("1, 3, 7", (1, 3, 7)), ("", ())],
)
def test_csv_retry_days_are_ints(raw: str, expected: tuple[int, ...]) -> None:
    got = build(SUBSCRIPTION_AUTO_CHARGE_RETRY_DAYS=raw).subscription_auto_charge_retry_days
    assert got == expected
    assert all(isinstance(d, int) for d in got)


def test_multi_currency_parsing() -> None:
    settings = build(SUPPORTED_CURRENCIES="USD,KHR", DEFAULT_CURRENCY="KHR")
    assert settings.supported_currencies == ("USD", "KHR")
    assert settings.default_currency == "KHR"


# ------------------------------------------------------------------- invariants


def test_default_currency_must_be_supported() -> None:
    with pytest.raises(ValidationError, match="not in SUPPORTED_CURRENCIES"):
        build(SUPPORTED_CURRENCIES="USD", DEFAULT_CURRENCY="KHR")


def test_currency_unknown_to_money_module_rejected() -> None:
    """A currency with no declared minor-unit count cannot be stored safely."""
    with pytest.raises(ValidationError, match="MINOR_UNITS"):
        build(SUPPORTED_CURRENCIES="USD,EUR")


def test_mastery_low_above_high_rejected() -> None:
    with pytest.raises(ValidationError, match="MASTERY_LOW"):
        build(MASTERY_LOW="90", MASTERY_HIGH="80")


def test_mastery_delta_base_zero_rejected() -> None:
    """It is a divisor; zero would raise inside the mastery calculation."""
    with pytest.raises(ValidationError):
        build(MASTERY_DELTA_BASE="0")


@pytest.mark.parametrize("hour", ["-1", "24"])
def test_quota_reset_hour_out_of_range_rejected(hour: str) -> None:
    with pytest.raises(ValidationError):
        build(QUOTA_RESET_HOUR_LOCAL=hour)


@pytest.mark.parametrize("rate", ["-0.1", "1.1"])
def test_sample_rate_outside_unit_interval_rejected(rate: str) -> None:
    with pytest.raises(ValidationError):
        build(PIPELINE_REVIEW_SAMPLE_RATE=rate)


def test_sm2_ease_min_below_one_rejected() -> None:
    """Ease under 1.0 would shrink the interval on every success."""
    with pytest.raises(ValidationError):
        build(SM2_EASE_MIN="0.9")


# ------------------------------------------------------------------- production


def test_prod_rejects_blank_signing_secret() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        build(ENV="prod", TELEGRAM_BOT_TOKEN="t", JWT_SECRET="")


def test_prod_rejects_a_secret_shorter_than_the_hash() -> None:
    """RFC 7518 section 3.2: an HS256 key below 32 bytes weakens the signature."""
    with pytest.raises(ValidationError, match="RFC 7518"):
        build(ENV="prod", TELEGRAM_BOT_TOKEN="t", JWT_SECRET="x" * 31)


def test_prod_rejects_whitespace_only_secret() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        build(ENV="prod", TELEGRAM_BOT_TOKEN="t", JWT_SECRET="   ")


def test_prod_accepts_populated_secrets() -> None:
    settings = build(ENV="prod", TELEGRAM_BOT_TOKEN="bot-token", JWT_SECRET=PROD_JWT_SECRET)
    assert settings.is_production is True


def test_dev_tolerates_blank_secrets() -> None:
    """All-fake local development must not require real credentials."""
    assert build(ENV="dev").is_production is False


def test_secrets_are_not_exposed_by_model_dump() -> None:
    dumped = repr(build(JWT_SECRET="super-secret").model_dump())
    assert "super-secret" not in dumped


# -------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("plan", "expected"),
    [("free", 10), ("basic", 0), ("pro", 0)],
)
def test_daily_attempt_limit(plan: Plan, expected: int) -> None:
    assert build().daily_attempt_limit(plan) == expected


@pytest.mark.parametrize(
    ("plan", "expected"),
    [("free", 15), ("basic", 45), ("pro", 230)],
)
def test_monthly_cost_cap_matches_prd(plan: Plan, expected: int) -> None:
    assert build().monthly_cost_cap_usd_cents(plan) == expected

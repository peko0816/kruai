"""Application settings.

One field per row of docs/CONFIG_REFERENCE.md, in the same order and under the
same section headings, so the two can be read side by side. Adding a field here
without adding it there is a PR reject — and tests/unit/test_config.py fails the
build if the two ever drift.

``extra="forbid"`` is the point of this module: a typo in .env must stop the
process, not silently fall back to a default. ``SCORING_PASS_THRESHOLDD=75``
would otherwise leave the real threshold at 60 and nobody would notice.

Provider *names* are plain strings, not Literals. The registry owns the set of
implementations that actually exist and validates the configured names at
startup (BACKLOG B5); duplicating that list here would create a second place to
update every time a provider lands.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.money import is_supported_currency

#: Repo root, resolved from this file rather than the CWD: ``make`` runs the
#: backend with ``--directory backend`` while .env lives at the root next to
#: docker-compose.yml, which reads the same file (docs/DECISIONS.md D-001).
#: backend/app/core/config.py -> core -> app -> backend -> <root>
_REPO_ROOT = Path(__file__).resolve().parents[3]

Plan = Literal["free", "basic", "pro"]


def _split_csv(value: Any) -> Any:
    """Turn ``"a, b"`` into ``("a", "b")``, leaving non-strings alone.

    pydantic-settings would otherwise try to JSON-decode env values for
    collection-typed fields and reject plain comma-separated input; the
    ``NoDecode`` annotation on each field suppresses that so this runs instead.
    """
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # ---------------------------------------------------------------- 1. 运行环境
    env: Literal["dev", "staging", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    database_url: str
    redis_url: str

    #: Optional until the content pipeline uploads real media. Making these
    #: required would break the all-fake configuration that gate G-B depends on
    #: (docs/DECISIONS.md D-002).
    object_storage_endpoint: str = ""
    object_storage_bucket: str = "kruai-media"
    public_media_base_url: str = ""

    # ------------------------------------------------------------ 2. Provider 选择
    scoring_provider: str = "fake"
    scoring_fallback_providers: Annotated[tuple[str, ...], NoDecode] = ()
    tts_provider: str = "fake"
    llm_provider: str = "fake"
    payment_providers: Annotated[tuple[str, ...], NoDecode] = ("fake",)
    realtime_provider: str = "fake"

    # ------------------------------------------------------- 3. 评分与掌握度 [M0]
    #: [M0-1] Recalibrate once the real score distribution is known. 60 is a
    #: placeholder, not a measured pass mark.
    scoring_pass_threshold: float = Field(default=60.0, ge=0.0, le=100.0)
    scoring_max_retry: int = Field(default=3, ge=0)
    scoring_timeout_ms: int = Field(default=8000, gt=0)

    #: Divisor in ``delta = (score - pass_threshold) / base``; zero would raise.
    mastery_delta_base: float = Field(default=40.0, gt=0.0)
    mastery_weight_drill: float = Field(default=0.6, ge=0.0)
    mastery_weight_vocab: float = Field(default=0.8, ge=0.0)
    mastery_weight_qa: float = Field(default=1.0, ge=0.0)
    mastery_high: float = Field(default=80.0, ge=0.0, le=100.0)
    mastery_low: float = Field(default=60.0, ge=0.0, le=100.0)

    sm2_ease_initial: float = Field(default=2.5, gt=0.0)
    #: Below 1.0 an interval would shrink on every success.
    sm2_ease_min: float = Field(default=1.3, ge=1.0)
    sm2_ease_penalty: float = Field(default=0.2, ge=0.0)

    #: [M0-1] Whether zh phoneme strings carry tone digits (parse) or tone must
    #: be inferred (derive). ``auto`` probes at runtime.
    zh_tone_mode: Literal["parse", "derive", "auto"] = "auto"

    # ------------------------------------------------------------- 4. 额度与限制
    limit_free_daily_tasks: int = Field(default=3, ge=0)
    limit_free_daily_attempts: int = Field(default=10, ge=0)
    #: 0 means unlimited.
    limit_basic_daily_attempts: int = Field(default=0, ge=0)
    limit_pro_realtime_seconds_monthly: int = Field(default=3600, ge=0)
    #: [$] Server-enforced hard disconnect for a single realtime session.
    realtime_session_max_seconds: int = Field(default=900, gt=0)
    #: [$] Summarise beyond this many turns so context stops growing linearly.
    realtime_context_summarize_after_turns: int = Field(default=8, gt=0)
    #: Hour in the user's own timezone at which daily counters reset.
    quota_reset_hour_local: int = Field(default=0, ge=0, le=23)

    # --------------------------------------------------------------- 5. 成本护栏 [$]
    #: The only float that touches money, and it is a ratio rather than an
    #: amount, so R4 holds. But the comparison it drives is not exact:
    #: 45 * 1.5 is 67.5, and whoever implements the guardrail has to decide
    #: which side of 67 counts as over. Round explicitly at that call site
    #: rather than letting float ordering decide it.
    cost_alert_multiplier: float = Field(default=1.5, gt=0.0)
    cost_cap_free_usd_cents_monthly: int = Field(default=15, ge=0)
    cost_cap_basic_usd_cents_monthly: int = Field(default=45, ge=0)
    cost_cap_pro_usd_cents_monthly: int = Field(default=230, ge=0)
    #: Must stay true in dev and CI: an unledgered external call should raise.
    cost_ledger_required: bool = True

    # ------------------------------------------------------------- 5b. 订阅与续费
    #: Fallback when the channel cannot do direct debit; the provider's
    #: supports_recurring decides the real path (ARCHITECTURE 3.4).
    subscription_default_renewal_mode: Literal["auto", "manual"] = "manual"
    subscription_reminder_days_before: int = Field(default=3, ge=0)
    subscription_grace_days: int = Field(default=3, ge=0)
    subscription_auto_charge_retry_days: Annotated[tuple[int, ...], NoDecode] = (1, 3)
    supported_currencies: Annotated[tuple[str, ...], NoDecode] = ("USD",)
    default_currency: str = "USD"

    # --------------------------------------------------------------- 6. 媒体与回退
    #: Keep false until the S2 entry conditions are met (PRD 7.2).
    media_video_enabled: bool = False
    media_video_min_plan: Plan = "pro"
    media_client_video_timeout_ms: int = Field(default=3000, gt=0)
    media_default_data_saver: bool = False

    # ----------------------------------------------------------------- 7. 内容管线
    #: [$] Video costs $1-4 per minute to regenerate. Stays false until S2.
    pipeline_with_video: bool = False
    pipeline_review_sample_rate: float = Field(default=0.10, ge=0.0, le=1.0)
    pipeline_min_sentences_per_concept: int = Field(default=8, ge=0)
    pipeline_min_substitutions_per_concept: int = Field(default=12, ge=0)
    pipeline_min_dialogues_per_concept: int = Field(default=3, ge=0)
    pipeline_dedup_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    pipeline_max_chars_hsk1: int = Field(default=8, gt=0)
    pipeline_max_chars_hsk2: int = Field(default=12, gt=0)
    pipeline_max_chars_hsk3: int = Field(default=18, gt=0)
    tts_speaking_rate_zh: float = Field(default=0.9, gt=0.0)
    tts_speaking_rate_km: float = Field(default=1.0, gt=0.0)

    # ------------------------------------------------------------- 8. 实验 (A/B)
    experiment_explain_media_enabled: bool = False
    experiment_explain_media_split: float = Field(default=0.5, ge=0.0, le=1.0)

    # ---------------------------------------------------------------------- 9. 密钥
    # No defaults, ever: a missing line must stop startup rather than let the
    # process run with a fabricated key. Credentials are SecretStr so that
    # model_dump() and repr cannot leak them into a log line; the identifiers
    # and URLs grouped here are not credentials and stay plain.
    telegram_bot_token: SecretStr
    azure_speech_key: SecretStr
    azure_speech_region: str
    google_application_credentials: str
    elevenlabs_api_key: SecretStr
    openai_api_key: SecretStr
    payway_merchant_id: str
    payway_api_key: SecretStr
    payway_base_url: str
    bakong_token: SecretStr
    jwt_secret: SecretStr

    # ------------------------------------------------------------------- validators

    @field_validator(
        "scoring_fallback_providers",
        "payment_providers",
        "subscription_auto_charge_retry_days",
        "supported_currencies",
        mode="before",
    )
    @classmethod
    def _parse_csv(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("supported_currencies")
    @classmethod
    def _currencies_known_to_money(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [c for c in value if not is_supported_currency(c)]
        if unknown:
            raise ValueError(
                f"{unknown} not in core.money.MINOR_UNITS; a currency without a "
                "declared minor-unit count cannot be formatted or stored safely"
            )
        return value

    @model_validator(mode="after")
    def _check_invariants(self) -> Settings:
        if self.default_currency not in self.supported_currencies:
            raise ValueError(
                f"DEFAULT_CURRENCY={self.default_currency!r} is not in "
                f"SUPPORTED_CURRENCIES={self.supported_currencies}"
            )
        if self.mastery_low > self.mastery_high:
            raise ValueError(
                f"MASTERY_LOW ({self.mastery_low}) must not exceed "
                f"MASTERY_HIGH ({self.mastery_high})"
            )
        if self.env == "prod":
            blank = [
                name
                for name in ("jwt_secret", "telegram_bot_token")
                if not getattr(self, name).get_secret_value().strip()
            ]
            if blank:
                raise ValueError(
                    f"{[n.upper() for n in blank]} must be set when ENV=prod; "
                    "an empty signing secret means forgeable tokens"
                )
        return self

    # ---------------------------------------------------------------- convenience

    @property
    def is_production(self) -> bool:
        return self.env == "prod"

    def daily_attempt_limit(self, plan: Plan) -> int:
        """Daily attempt allowance for a plan; 0 means unlimited."""
        if plan == "free":
            return self.limit_free_daily_attempts
        if plan == "basic":
            return self.limit_basic_daily_attempts
        return 0

    def daily_task_limit(self, plan: Plan) -> int:
        """Daily task allowance for a plan; 0 means unlimited.

        Only Free is capped (PRD 4.3), so there is no LIMIT_BASIC_DAILY_TASKS to
        read — paid plans return the unlimited sentinel rather than a number.
        """
        return self.limit_free_daily_tasks if plan == "free" else 0

    def monthly_cost_cap_usd_cents(self, plan: Plan) -> int:
        """Per-user monthly cost ceiling from PRD section 11.2."""
        caps: dict[str, int] = {
            "free": self.cost_cap_free_usd_cents_monthly,
            "basic": self.cost_cap_basic_usd_cents_monthly,
            "pro": self.cost_cap_pro_usd_cents_monthly,
        }
        return caps[plan]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once.

    Cached rather than module-level so that importing this module never touches
    the filesystem — tests construct their own Settings, and a missing .env
    should fail where it is used, not at import time.
    """
    return Settings()

"""initData verification and session tokens.

The tests are written from the attacker's side: almost every case here is a
string that must NOT authenticate. A test suite for a signature check that only
exercises the valid case proves that the happy path works and nothing else —
and the only interesting property of this module is what it refuses.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import uuid
from typing import Any
from urllib.parse import urlencode

import jwt
import pytest
from pydantic import ValidationError

from app.core.config import MIN_JWT_SECRET_LENGTH, Settings
from app.core.errors import AuthenticationFailed
from app.core.security import (
    JWT_ALGORITHM,
    JWT_ISSUER,
    decode_access_token,
    issue_access_token,
    normalize_locale,
    verify_init_data,
)

BOT_TOKEN = "123456:test-bot-token"
NOW = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.UTC)
MAX_AGE = 3600

_MINIMAL_SETTINGS: dict[str, Any] = {
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
    "AZURE_SPEECH_KEY": "",
    "AZURE_SPEECH_REGION": "",
    "GOOGLE_APPLICATION_CREDENTIALS": "",
    "ELEVENLABS_API_KEY": "",
    "OPENAI_API_KEY": "",
    "PAYWAY_MERCHANT_ID": "",
    "PAYWAY_API_KEY": "",
    "PAYWAY_BASE_URL": "",
    "BAKONG_TOKEN": "",
    # 32 characters: the RFC 7518 floor the module enforces.
    "JWT_SECRET": "unit-test-signing-key-0123456789",
}


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {**_MINIMAL_SETTINGS, **overrides}
    return Settings(_env_file=None, **values)


def sign(fields: dict[str, str], *, bot_token: str = BOT_TOKEN) -> dict[str, str]:
    """Attach the hash Telegram would attach to these exact fields."""
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return {**fields, "hash": digest}


def init_data(
    *,
    telegram_id: int = 4242,
    language_code: str | None = "km",
    auth_date: datetime.datetime = NOW,
    bot_token: str = BOT_TOKEN,
    extra: dict[str, str] | None = None,
    user_override: Any = None,
) -> str:
    """A signed initData string, as Telegram's client would produce it."""
    user: Any
    if user_override is not None:
        user = user_override
    else:
        user = {"id": telegram_id}
        if language_code is not None:
            user["language_code"] = language_code

    fields = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAF-test",
        "user": user if isinstance(user, str) else json.dumps(user, separators=(",", ":")),
        **(extra or {}),
    }
    return urlencode(sign(fields, bot_token=bot_token))


def verify(raw: str, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "bot_token": BOT_TOKEN,
        "now": NOW,
        "max_age_seconds": MAX_AGE,
        **overrides,
    }
    return verify_init_data(raw, **kwargs)


# ------------------------------------------------------------------ the valid case


def test_genuine_init_data_yields_the_telegram_account() -> None:
    identity = verify(init_data(telegram_id=987654321, language_code="km"))

    assert identity.telegram_id == 987654321
    assert identity.locale == "km"
    assert identity.auth_date == NOW


def test_unknown_fields_stay_inside_the_signature() -> None:
    """Telegram adds fields over time; new ones are signed like any other.

    ``signature`` is the live example: it belongs to their Ed25519 scheme and
    is part of the HMAC data-check string all the same. Dropping it from the
    chain — a plausible reading of their docs — breaks every real sign-in.
    """
    raw = init_data(extra={"signature": "abc_def", "chat_type": "private"})

    assert verify(raw).telegram_id == 4242


# ------------------------------------------------------------------- forgeries


def test_tampered_field_is_rejected() -> None:
    """The classic: keep the hash, swap the account it vouches for."""
    raw = init_data(telegram_id=1).replace("%22id%22%3A1", "%22id%22%3A2")

    with pytest.raises(AuthenticationFailed):
        verify(raw)


def test_hash_from_a_different_bot_token_is_rejected() -> None:
    with pytest.raises(AuthenticationFailed):
        verify(init_data(bot_token="999:someone-elses-bot"))


def test_unsigned_init_data_is_rejected() -> None:
    raw = urlencode({"auth_date": str(int(NOW.timestamp())), "user": '{"id":1}'})

    with pytest.raises(AuthenticationFailed):
        verify(raw)


def test_blank_hash_is_rejected() -> None:
    with pytest.raises(AuthenticationFailed):
        verify(f"auth_date={int(NOW.timestamp())}&user=%7B%22id%22%3A1%7D&hash=")


def test_appended_unsigned_field_is_rejected() -> None:
    """A field added after signing changes the chain, so the hash no longer fits."""
    with pytest.raises(AuthenticationFailed):
        verify(init_data() + "&is_premium=true")


def test_removed_field_is_rejected() -> None:
    raw = init_data()
    stripped = "&".join(p for p in raw.split("&") if not p.startswith("query_id="))

    with pytest.raises(AuthenticationFailed):
        verify(stripped)


def test_duplicate_key_is_rejected_for_being_ambiguous() -> None:
    """Two values for one key: refused before anything interprets either.

    The reason is asserted, not just the refusal. A duplicate would fail the
    signature check anyway — whichever value survived ``dict()``, the chain no
    longer matches — so without pinning *why*, deleting the guard changes
    nothing any test can see.
    """
    with pytest.raises(AuthenticationFailed) as exc:
        verify(init_data() + "&user=%7B%22id%22%3A9%7D")

    assert exc.value.context["reason"] == "init_data_duplicate_key"


@pytest.mark.parametrize("raw", ["", "not-a-query-string", "&", "a=1&&b=2"])
def test_malformed_input_is_rejected(raw: str) -> None:
    with pytest.raises(AuthenticationFailed):
        verify(raw)


def test_every_rejection_reports_the_same_code() -> None:
    """Which check failed is log detail, not a response field."""
    with pytest.raises(AuthenticationFailed) as exc:
        verify(init_data(bot_token="wrong"))

    assert exc.value.code == "auth.invalid"
    assert exc.value.http_status == 401
    assert "hash" in str(exc.value.context["reason"])
    assert exc.value.message == "auth.invalid"


# ------------------------------------------------------------------- freshness


def test_init_data_older_than_the_window_is_rejected() -> None:
    stale = NOW - datetime.timedelta(seconds=MAX_AGE + 1)

    with pytest.raises(AuthenticationFailed):
        verify(init_data(auth_date=stale))


def test_init_data_exactly_at_the_window_is_accepted() -> None:
    edge = NOW - datetime.timedelta(seconds=MAX_AGE)

    assert verify(init_data(auth_date=edge)).telegram_id == 4242


def test_future_auth_date_is_accepted() -> None:
    """Only Telegram can sign it, so ahead of us means their clock is ahead."""
    ahead = NOW + datetime.timedelta(seconds=120)

    assert verify(init_data(auth_date=ahead)).telegram_id == 4242


@pytest.mark.parametrize("value", ["", "not-a-number", "1.5", "99999999999999999999"])
def test_malformed_auth_date_is_rejected(value: str) -> None:
    raw = urlencode(sign({"auth_date": value, "user": '{"id":1}'}))

    with pytest.raises(AuthenticationFailed):
        verify(raw)


def test_missing_auth_date_is_rejected() -> None:
    raw = urlencode(sign({"user": '{"id":1}'}))

    with pytest.raises(AuthenticationFailed):
        verify(raw)


# ----------------------------------------------------------------- the user field


def test_init_data_without_a_user_is_rejected() -> None:
    """A signed channel or inline context: real, but nobody to issue a token to."""
    raw = urlencode(sign({"auth_date": str(int(NOW.timestamp())), "query_id": "AAF"}))

    with pytest.raises(AuthenticationFailed):
        verify(raw)


@pytest.mark.parametrize(
    "user",
    [
        "not json at all",
        "[1, 2, 3]",
        "42",
        '{"language_code": "km"}',
        '{"id": "4242"}',
        '{"id": true}',
        '{"id": 0}',
        '{"id": -7}',
        '{"id": 1.5}',
        '{"id": null}',
    ],
)
def test_unusable_user_object_is_rejected(user: str) -> None:
    with pytest.raises(AuthenticationFailed):
        verify(init_data(user_override=user))


def test_non_string_language_code_falls_back_rather_than_failing() -> None:
    """A junk language tag is not an authentication problem, just a bad guess."""
    raw = init_data(user_override='{"id": 88, "language_code": 5}')

    identity = verify(raw)

    assert identity.telegram_id == 88
    assert identity.locale == "km"


# --------------------------------------------------------------------- locales


@pytest.mark.parametrize(
    ("language_code", "expected"),
    [
        ("km", "km"),
        ("zh", "zh"),
        ("zh-hans", "zh"),
        ("zh-Hant", "zh"),
        ("en", "en"),
        ("en-US", "en"),
        ("EN", "en"),
        (" km ", "km"),
        ("ru", "km"),
        ("th", "km"),
        ("", "km"),
        (None, "km"),
    ],
)
def test_locale_normalisation(language_code: str | None, expected: str) -> None:
    assert normalize_locale(language_code) == expected


def test_locale_comes_from_the_signed_language_code() -> None:
    assert verify(init_data(language_code="zh-hans")).locale == "zh"


# -------------------------------------------------------------- misconfiguration


@pytest.mark.parametrize("token", ["", "   "])
def test_blank_bot_token_refuses_to_verify_anything(token: str) -> None:
    """Not a rejected login — a server that cannot tell forgeries apart.

    HMAC with an empty key is still a working HMAC, so without this guard a
    deployment that forgot TELEGRAM_BOT_TOKEN would accept initData anyone
    could sign. ENV=dev ships the token blank, so this cannot be caught at boot.
    """
    forged = init_data(bot_token=token)

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        verify(forged, bot_token=token)


def test_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        verify(init_data(), now=datetime.datetime(2026, 3, 1, 12, 0))


# ------------------------------------------------------------------ access tokens


def now_utc() -> datetime.datetime:
    """Real time, because PyJWT checks exp against the real clock on decode.

    Verification tests use the frozen NOW above; token tests cannot, or every
    round trip would decode a token that expired months ago.
    """
    return datetime.datetime.now(datetime.UTC)


def test_token_round_trips_to_the_user_id() -> None:
    config = settings()
    user_id = uuid.uuid4()
    issued_at = now_utc()

    token = issue_access_token(user_id, settings=config, now=issued_at)

    assert decode_access_token(token.token, settings=config) == user_id
    assert token.expires_in == config.jwt_access_token_ttl_seconds
    assert token.expires_at == issued_at + datetime.timedelta(seconds=token.expires_in)


def test_ttl_comes_from_configuration() -> None:
    config = settings(JWT_ACCESS_TOKEN_TTL_SECONDS="60")

    token = issue_access_token(uuid.uuid4(), settings=config, now=NOW)

    assert token.expires_in == 60


def test_token_carries_identity_and_nothing_else() -> None:
    """A plan or quota baked into a token is an entitlement nobody can revoke."""
    config = settings()

    token = issue_access_token(uuid.uuid4(), settings=config, now=now_utc())
    claims = jwt.decode(
        token.token,
        config.jwt_secret.get_secret_value(),
        algorithms=[JWT_ALGORITHM],
        issuer=JWT_ISSUER,
    )

    assert set(claims) == {"sub", "iss", "iat", "exp"}


def test_expired_token_is_rejected() -> None:
    config = settings()
    long_ago = now_utc() - datetime.timedelta(days=1)

    token = issue_access_token(uuid.uuid4(), settings=config, now=long_ago)

    with pytest.raises(AuthenticationFailed):
        decode_access_token(token.token, settings=config)


def test_token_signed_with_another_secret_is_rejected() -> None:
    token = issue_access_token(
        uuid.uuid4(),
        settings=settings(JWT_SECRET="their-key-padded-to-thirty-two-ch"),
        now=now_utc(),
    )

    with pytest.raises(AuthenticationFailed):
        decode_access_token(
            token.token, settings=settings(JWT_SECRET="our-key-padded-to-thirty-two-chrs")
        )


def test_token_from_another_issuer_is_rejected() -> None:
    """Our secret in a shared deployment is not a licence to mint our sessions."""
    config = settings()
    claims = {
        "sub": str(uuid.uuid4()),
        "iss": "someone-else",
        "iat": int(now_utc().timestamp()),
        "exp": int((now_utc() + datetime.timedelta(hours=1)).timestamp()),
    }
    forged = jwt.encode(claims, config.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)

    with pytest.raises(AuthenticationFailed):
        decode_access_token(forged, settings=config)


def test_unsigned_alg_none_token_is_rejected() -> None:
    """The alg-confusion classic: relabel the header, drop the signature."""
    claims = {
        "sub": str(uuid.uuid4()),
        "iss": JWT_ISSUER,
        "iat": int(now_utc().timestamp()),
        "exp": int((now_utc() + datetime.timedelta(hours=1)).timestamp()),
    }
    forged = jwt.encode(claims, key="", algorithm="none")

    with pytest.raises(AuthenticationFailed):
        decode_access_token(forged, settings=settings())


def test_token_without_an_expiry_is_rejected() -> None:
    """PyJWT validates the claims present; a missing exp would never expire."""
    config = settings()
    claims = {"sub": str(uuid.uuid4()), "iss": JWT_ISSUER, "iat": int(now_utc().timestamp())}
    forever = jwt.encode(claims, config.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)

    with pytest.raises(AuthenticationFailed):
        decode_access_token(forever, settings=config)


def test_token_whose_subject_is_not_a_uuid_is_rejected() -> None:
    config = settings()
    claims = {
        "sub": "admin",
        "iss": JWT_ISSUER,
        "iat": int(now_utc().timestamp()),
        "exp": int((now_utc() + datetime.timedelta(hours=1)).timestamp()),
    }
    forged = jwt.encode(claims, config.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)

    with pytest.raises(AuthenticationFailed):
        decode_access_token(forged, settings=config)


@pytest.mark.parametrize("garbage", ["", "not.a.token", "a.b.c", "Bearer xyz"])
def test_garbage_is_rejected(garbage: str) -> None:
    with pytest.raises(AuthenticationFailed):
        decode_access_token(garbage, settings=settings())


@pytest.mark.parametrize("secret", ["", "   "])
def test_blank_jwt_secret_refuses_to_sign_or_verify(secret: str) -> None:
    config = settings(JWT_SECRET=secret)

    with pytest.raises(ValueError, match="JWT_SECRET is empty"):
        issue_access_token(uuid.uuid4(), settings=config, now=NOW)
    with pytest.raises(ValueError, match="JWT_SECRET is empty"):
        decode_access_token("whatever", settings=config)


@pytest.mark.parametrize("secret", ["short", "x" * (MIN_JWT_SECRET_LENGTH - 1)])
def test_short_jwt_secret_refuses_to_sign(secret: str) -> None:
    """RFC 7518 section 3.2. PyJWT only warns, and a warning is not a control."""
    config = settings(JWT_SECRET=secret)

    with pytest.raises(ValueError, match="RFC 7518"):
        issue_access_token(uuid.uuid4(), settings=config, now=NOW)


def test_secret_at_exactly_the_floor_is_accepted() -> None:
    config = settings(JWT_SECRET="y" * MIN_JWT_SECRET_LENGTH)
    user_id = uuid.uuid4()

    token = issue_access_token(user_id, settings=config, now=now_utc())

    assert decode_access_token(token.token, settings=config) == user_id


def test_production_refuses_to_start_with_a_short_secret() -> None:
    """Caught at boot, not at the first sign-in, when ENV says it matters."""
    with pytest.raises(ValidationError, match="RFC 7518"):
        settings(ENV="prod", JWT_SECRET="too-short")

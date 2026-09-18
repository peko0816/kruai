"""Who the caller is: Telegram initData in, a session token out.

Two independent pieces of cryptography, both here because both answer the same
question and neither touches the database.

**initData** is what a Telegram Mini App hands the client. It is a query string
signed by Telegram with a key derived from the bot token, so a server holding
that token can tell a real Telegram session from a fabricated one. The client
must forward it verbatim (CODING_STANDARDS section 12) — a client that parsed it
first and sent us the parts has sent us nothing but its own claims.

**The session token** is a JWT we sign ourselves, so that the rest of the API
does not re-verify initData on every request.

Three properties are load-bearing and each has a test that fails without it:

  1. The signature is checked *before* any field is interpreted. Nothing below
     ``_verify_signature`` parses attacker-controlled input.
  2. An empty signing key raises rather than authenticating. HMAC with a blank
     key still produces a perfectly good digest — one anybody can compute — so a
     deployment that forgot TELEGRAM_BOT_TOKEN would not fail, it would accept
     forgeries. ENV=dev ships blank on purpose (CI runs with no credentials),
     so this cannot be a startup check; it has to be here.
  3. The token names its algorithm and we ignore it. ``algorithms=[HS256]`` is
     what stops the alg-confusion family, where an attacker re-labels a token
     ``{"alg": "none"}`` and drops the signature.

The failure reason never reaches the client. Every rejection is the same code
and the same message; which check failed is in the log, where the operator can
see it and the forger cannot.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import parse_qsl

import jwt

from app.core.config import MIN_JWT_SECRET_LENGTH, Settings
from app.core.errors import AuthenticationFailed

#: Telegram derives the signing key as HMAC(key="WebAppData", msg=bot_token).
#: Fixed by their protocol, not a choice of ours.
_SECRET_KEY_SALT: Final = b"WebAppData"

#: Excluded from the data-check string because it *is* the digest. Everything
#: else Telegram sent stays in, including ``signature`` — that field is part of
#: the signed chain for HMAC validation and only comes out for the Ed25519
#: third-party check, which we do not do.
_HASH_FIELD: Final = "hash"

JWT_ALGORITHM: Final = "HS256"

#: Pinned into every token and required on the way back, so a token minted by
#: some other service that happens to share our secret cannot be spent here.
#: Not configuration: changing it invalidates every live session, which is a
#: code change with a migration story, not a knob.
JWT_ISSUER: Final = "kruai"

#: users.locale in DATA_MODEL.sql. Telegram sends IETF tags ("zh-hans",
#: "en-US"), so the subtag is dropped and the primary tag matched.
SUPPORTED_LOCALES: Final[frozenset[str]] = frozenset({"km", "zh", "en"})

#: Khmer is the teaching language (PRD 1.1), so an unrecognised Telegram
#: language lands there rather than on English.
DEFAULT_LOCALE: Final = "km"


@dataclass(frozen=True)
class TelegramIdentity:
    """A Telegram account, proven by a signature we checked ourselves.

    Deliberately narrow: initData also carries first name, last name, photo URL
    and a chat instance, and ``users`` has a column for none of them. Carrying
    personal data past the point where it is needed is how it ends up in a log.
    """

    telegram_id: int
    locale: str
    #: When Telegram issued this initData. Kept for the freshness log line.
    auth_date: datetime.datetime


@dataclass(frozen=True)
class AccessToken:
    """A signed session token and when it stops working."""

    token: str
    #: Seconds from issue to expiry — what an OAuth-style client expects.
    expires_in: int
    expires_at: datetime.datetime


def verify_init_data(
    raw: str, *, bot_token: str, now: datetime.datetime, max_age_seconds: int
) -> TelegramIdentity:
    """Check a Mini App's initData and return who it says the caller is.

    Args:
        raw: the initData string exactly as Telegram gave the client.
        bot_token: TELEGRAM_BOT_TOKEN. Blank is a configuration fault, not a
            rejected login.
        now: timezone-aware; the reference point for the freshness window.
        max_age_seconds: how old initData may be. Bounds the replay window for
            a string that leaked out of a client.

    Raises:
        AuthenticationFailed: any reason at all. The caller gets one answer.
        ValueError: the bot token is empty, or ``now`` is naive — both mean this
            process cannot authenticate anyone and should say so loudly.
    """
    if not bot_token.strip():
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is empty; initData would be verified against a "
            "key anyone can reproduce, so every forgery would be accepted"
        )
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware to be compared with auth_date")

    fields = _parse_fields(raw)
    _verify_signature(fields, bot_token=bot_token)

    # Past this line the content is Telegram's, not the caller's.
    auth_date = _read_auth_date(fields, now=now, max_age_seconds=max_age_seconds)
    telegram_id, language_code = _read_user(fields)

    return TelegramIdentity(
        telegram_id=telegram_id,
        locale=normalize_locale(language_code),
        auth_date=auth_date,
    )


def normalize_locale(language_code: str | None) -> str:
    """Map a Telegram language tag onto the three locales we ship.

    ``zh-hans`` and ``zh-hant`` both become ``zh``: the difference is script,
    and our Chinese strings are one file. Anything unrecognised becomes Khmer.
    """
    if not language_code:
        return DEFAULT_LOCALE
    primary = language_code.strip().lower().split("-")[0]
    return primary if primary in SUPPORTED_LOCALES else DEFAULT_LOCALE


def issue_access_token(
    user_id: uuid.UUID, *, settings: Settings, now: datetime.datetime
) -> AccessToken:
    """Sign a session token for this user.

    The only claim about the user is their id. Plan, quota and locale are
    deliberately absent: they change while a token lives, and a client holding
    a token that says ``plan=pro`` would be holding an entitlement no revocation
    can reach. Everything but identity is read from the database per request.
    """
    ttl = settings.jwt_access_token_ttl_seconds
    expires_at = now + datetime.timedelta(seconds=ttl)
    claims = {
        "sub": str(user_id),
        "iss": JWT_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(claims, _signing_secret(settings), algorithm=JWT_ALGORITHM)
    return AccessToken(token=token, expires_in=ttl, expires_at=expires_at)


def decode_access_token(token: str, *, settings: Settings) -> uuid.UUID:
    """The user id a token proves, or a refusal.

    ``require`` is listed explicitly because PyJWT only validates the claims a
    token actually carries: a token with no ``exp`` is, by default, a token that
    never expires.

    Raises:
        AuthenticationFailed: expired, tampered with, signed by someone else,
            issued by something else, or carrying a subject that is not a uuid.
        ValueError: JWT_SECRET is empty.
    """
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            _signing_secret(settings),
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        raise AuthenticationFailed(reason=f"jwt_{type(exc).__name__}") from exc

    try:
        return uuid.UUID(str(claims["sub"]))
    except ValueError as exc:
        raise AuthenticationFailed(reason="jwt_subject_not_a_uuid") from exc


# ----------------------------------------------------------------------- internals


def _signing_secret(settings: Settings) -> str:
    """JWT_SECRET, or a refusal to sign anything at all.

    Two failures, both configuration rather than authentication, so both raise
    instead of returning a rejected login. An empty key signs tokens anybody can
    reproduce. A short one is what PyJWT warns about under RFC 7518 section 3.2
    — and a warning is not a control, so this turns it into a refusal.
    """
    secret = settings.jwt_secret.get_secret_value()
    if not secret.strip():
        raise ValueError(
            "JWT_SECRET is empty; tokens would be signed with a key anyone can "
            "reproduce, which makes every session forgeable"
        )
    if len(secret) < MIN_JWT_SECRET_LENGTH:
        raise ValueError(
            f"JWT_SECRET is {len(secret)} characters; HS256 needs at least "
            f"{MIN_JWT_SECRET_LENGTH} (RFC 7518 section 3.2). "
            "Generate one with: openssl rand -hex 32"
        )
    return secret


def _parse_fields(raw: str) -> dict[str, str]:
    """initData as a mapping, refusing anything ambiguous.

    ``strict_parsing`` rejects malformed pairs instead of dropping them, and
    duplicate keys are rejected outright: ``dict()`` would keep one of the two
    while the signature was computed over both, so whichever we kept, some pair
    of values would verify that should not.
    """
    try:
        pairs = parse_qsl(raw, strict_parsing=True, keep_blank_values=True)
    except ValueError as exc:
        raise AuthenticationFailed(reason="init_data_unparseable") from exc

    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise AuthenticationFailed(reason="init_data_duplicate_key")
    return fields


def _verify_signature(fields: dict[str, str], *, bot_token: str) -> None:
    provided = fields.get(_HASH_FIELD)
    if not provided:
        raise AuthenticationFailed(reason="hash_missing")

    check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items()) if key != _HASH_FIELD
    )
    secret_key = hmac.new(_SECRET_KEY_SALT, bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, provided):
        raise AuthenticationFailed(reason="hash_mismatch")


def _read_auth_date(
    fields: dict[str, str], *, now: datetime.datetime, max_age_seconds: int
) -> datetime.datetime:
    """Issue time, rejected once it is older than the window.

    Only the past is bounded. initData dated in the future can only come from
    Telegram's clock running ahead of ours — it carries a valid signature, so
    nobody else could have produced it — and rejecting it would turn their clock
    skew into our outage.
    """
    raw = fields.get("auth_date")
    if raw is None:
        raise AuthenticationFailed(reason="auth_date_missing")
    try:
        auth_date = datetime.datetime.fromtimestamp(int(raw), tz=datetime.UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise AuthenticationFailed(reason="auth_date_malformed") from exc

    if (now - auth_date).total_seconds() > max_age_seconds:
        raise AuthenticationFailed(reason="auth_date_stale")
    return auth_date


def _read_user(fields: dict[str, str]) -> tuple[int, str | None]:
    """The Telegram account id and language tag out of the ``user`` field.

    initData without a ``user`` is a real shape — an inline-mode or channel
    context sends ``receiver`` or ``chat`` instead — but not one this endpoint
    serves: there is nobody to sign a token for.
    """
    raw = fields.get("user")
    if raw is None:
        raise AuthenticationFailed(reason="user_missing")
    try:
        user = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuthenticationFailed(reason="user_not_json") from exc
    if not isinstance(user, dict):
        raise AuthenticationFailed(reason="user_not_an_object")

    telegram_id = user.get("id")
    # bool is an int in Python, and ``{"id": true}`` would otherwise become
    # account number 1.
    if not isinstance(telegram_id, int) or isinstance(telegram_id, bool) or telegram_id <= 0:
        raise AuthenticationFailed(reason="user_id_invalid")

    language_code = user.get("language_code")
    if language_code is not None and not isinstance(language_code, str):
        language_code = None
    return telegram_id, language_code

"""AppError carries an API contract: code drives client-side i18n."""

from __future__ import annotations

import pytest

from app.core.errors import AppError, InsufficientQuota, NotSubscribed


def test_quota_error_maps_to_payment_required() -> None:
    error = InsufficientQuota()
    assert error.code == "quota.insufficient"
    assert error.http_status == 402


def test_subscription_error_maps_to_payment_required() -> None:
    error = NotSubscribed()
    assert error.code == "sub.required"
    assert error.http_status == 402


def test_codes_are_unique() -> None:
    """A duplicated code would make two conditions indistinguishable to clients."""
    codes = [cls.code for cls in (InsufficientQuota, NotSubscribed)]
    assert len(codes) == len(set(codes))


def test_message_defaults_to_code() -> None:
    assert InsufficientQuota().message == "quota.insufficient"


def test_context_is_available_for_structured_logging() -> None:
    error = InsufficientQuota("daily attempts exhausted", used=10, limit=10)
    assert error.message == "daily attempts exhausted"
    assert error.context == {"used": 10, "limit": 10}


def test_subclasses_are_catchable_as_apperror() -> None:
    """The API layer catches the base class to map http_status generically."""
    with pytest.raises(AppError) as exc:
        raise NotSubscribed("pro plan required", plan="free")

    assert exc.value.http_status == 402
    assert exc.value.context == {"plan": "free"}


def test_repr_includes_code() -> None:
    assert "quota.insufficient" in repr(InsufficientQuota())

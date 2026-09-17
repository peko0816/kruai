"""Reset times across timezones and daylight-saving boundaries.

BACKLOG C5's acceptance names both. Neither fails loudly: a reset an hour off
just arrives at the wrong time, and a learner in one region gets their allowance
back mid-evening while everyone else is fine.

The daylight-saving cases use real zones on real dates rather than a contrived
offset, because the behaviour being relied on is Python's, and a made-up
timezone would only test the test.

  America/Santiago 2026-09-06 — local midnight does not exist; clocks jump
                                straight from 23:59 to 01:00.
  Asia/Beirut      2025-03-30 — the same gap.
  America/Havana   2026-11-01 — local midnight happens twice.

These pin fold=0's behaviour deliberately. It is Python's default and it happens
to be right for a quota reset in both directions, so nothing in the code says
"fold" at all — which is exactly why removing or overriding it would otherwise
go unnoticed.
"""

from __future__ import annotations

import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.core.config import Settings
from app.services.entitlements import load_timezone, next_reset_at

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

PHNOM_PENH = "Asia/Phnom_Penh"


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def settings(**overrides: str) -> Settings:
    values: dict[str, Any] = {**_MINIMAL, **overrides}
    return Settings(_env_file=None, **values)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC)


def local_reading(moment: datetime.datetime, zone: str) -> str:
    return moment.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------------ the common case


def test_the_reset_lands_on_local_midnight() -> None:
    """Cambodia is UTC+7, so local midnight is 17:00 UTC the previous day."""
    result = next_reset_at(utc(2026, 9, 17, 3, 0), timezone=PHNOM_PENH, settings=settings())

    assert local_reading(result, PHNOM_PENH) == "2026-09-18 00:00"
    assert result == utc(2026, 9, 17, 17, 0)


def test_the_next_reset_is_always_in_the_future() -> None:
    """Returning the boundary instant itself would leave the row due again
    immediately, and the reset would fire in a loop."""
    config = settings()
    boundary = utc(2026, 9, 17, 17, 0)  # exactly local midnight in Phnom Penh

    assert next_reset_at(boundary, timezone=PHNOM_PENH, settings=config) > boundary


def test_a_minute_before_the_boundary_still_points_at_tonight() -> None:
    """Local 23:59 must resolve to the midnight one minute away, not the one a
    day later — the off-by-one that would cost a learner a whole day."""
    result = next_reset_at(utc(2026, 9, 17, 16, 59), timezone=PHNOM_PENH, settings=settings())

    assert result == utc(2026, 9, 17, 17, 0)
    assert result - utc(2026, 9, 17, 16, 59) == datetime.timedelta(minutes=1)


def test_a_minute_after_the_boundary_points_at_tomorrow() -> None:
    result = next_reset_at(utc(2026, 9, 17, 17, 1), timezone=PHNOM_PENH, settings=settings())
    assert local_reading(result, PHNOM_PENH) == "2026-09-19 00:00"


@pytest.mark.parametrize(
    ("zone", "expected_local"),
    [
        ("Asia/Phnom_Penh", "2026-09-18 00:00"),
        ("UTC", "2026-09-18 00:00"),
        ("America/New_York", "2026-09-18 00:00"),
        ("Pacific/Kiritimati", "2026-09-19 00:00"),  # UTC+14: already tomorrow locally
        ("Pacific/Niue", "2026-09-18 00:00"),  # UTC-11
    ],
)
def test_every_learner_resets_at_their_own_midnight(zone: str, expected_local: str) -> None:
    """The point of storing a timezone per user: one global instant would hand
    the allowance back mid-evening for half the world."""
    result = next_reset_at(utc(2026, 9, 17, 12, 0), timezone=zone, settings=settings())
    assert local_reading(result, zone) == expected_local


def test_two_zones_reset_at_different_instants() -> None:
    """Twenty-five hours apart on the clock, so their resets cannot coincide —
    and each still lands on its own local midnight."""
    config = settings()
    moment = utc(2026, 9, 17, 12, 0)

    east = next_reset_at(moment, timezone="Pacific/Kiritimati", settings=config)
    west = next_reset_at(moment, timezone="Pacific/Niue", settings=config)

    assert east != west
    assert local_reading(east, "Pacific/Kiritimati").endswith("00:00")
    assert local_reading(west, "Pacific/Niue").endswith("00:00")


def test_the_reset_hour_comes_from_configuration() -> None:
    """QUOTA_RESET_HOUR_LOCAL, not a hardcoded midnight."""
    result = next_reset_at(
        utc(2026, 9, 17, 3, 0),  # local 10:00, so today's 04:00 has passed
        timezone=PHNOM_PENH,
        settings=settings(QUOTA_RESET_HOUR_LOCAL="4"),
    )
    assert local_reading(result, PHNOM_PENH) == "2026-09-18 04:00"


def test_a_reset_hour_later_today_is_not_pushed_to_tomorrow() -> None:
    config = settings(QUOTA_RESET_HOUR_LOCAL="20")
    result = next_reset_at(utc(2026, 9, 17, 3, 0), timezone=PHNOM_PENH, settings=config)

    assert local_reading(result, PHNOM_PENH) == "2026-09-17 20:00"


# ------------------------------------------------- daylight saving: the gap


def test_a_skipped_local_midnight_resets_at_the_first_real_instant() -> None:
    """Santiago jumps 23:59 to 01:00, so 00:00 never happens that day. The
    allowance has to come back at the moment the clock passes the reset hour,
    not vanish for a day."""
    result = next_reset_at(utc(2026, 9, 5, 12, 0), timezone="America/Santiago", settings=settings())

    assert local_reading(result, "America/Santiago") == "2026-09-06 01:00"
    assert result == utc(2026, 9, 6, 4, 0)


def test_the_same_gap_in_another_region() -> None:
    """Beirut has the same shape a different month, so this is the rule rather
    than one zone's quirk."""
    result = next_reset_at(utc(2025, 3, 29, 12, 0), timezone="Asia/Beirut", settings=settings())
    assert local_reading(result, "Asia/Beirut") == "2025-03-30 01:00"


def test_a_midnight_transition_shifts_the_reset_not_the_spacing() -> None:
    """Where the clocks move at midnight, it is the reset instant that slides —
    to local 01:00 — while consecutive resets stay a plain 24 hours apart."""
    config = settings()
    before = next_reset_at(utc(2026, 9, 4, 12, 0), timezone="America/Santiago", settings=config)
    across = next_reset_at(utc(2026, 9, 5, 12, 0), timezone="America/Santiago", settings=config)

    assert across - before == datetime.timedelta(hours=24)
    assert local_reading(before, "America/Santiago") == "2026-09-05 00:00"
    assert local_reading(across, "America/Santiago") == "2026-09-06 01:00"


# --------------------------------------------- daylight saving: the repeat


def test_a_doubled_local_midnight_resets_at_the_first_of_the_two() -> None:
    """Havana's midnight happens twice. Taking the second would leave the
    learner an hour short of their allowance for no reason."""
    result = next_reset_at(utc(2026, 10, 31, 12, 0), timezone="America/Havana", settings=settings())

    assert result == utc(2026, 11, 1, 4, 0)
    later_of_the_two = utc(2026, 11, 1, 5, 0)
    assert result < later_of_the_two


def test_spring_forward_puts_two_resets_twenty_three_hours_apart() -> None:
    """New York moves its clocks at 02:00, after midnight, so consecutive local
    midnights really are 23 hours apart in UTC. Adding a fixed day to the
    previous reset instead of rebuilding it from the calendar date would land
    this one at local 23:00 the day before."""
    config = settings()
    before = next_reset_at(utc(2026, 3, 7, 12, 0), timezone="America/New_York", settings=config)
    across = next_reset_at(before, timezone="America/New_York", settings=config)

    assert across - before == datetime.timedelta(hours=23)
    assert local_reading(across, "America/New_York") == "2026-03-09 00:00"


def test_falling_back_puts_two_resets_twenty_five_hours_apart() -> None:
    """The mirror image, and the one a fixed-day addition lands at 01:00."""
    config = settings()
    before = next_reset_at(utc(2026, 10, 31, 12, 0), timezone="America/New_York", settings=config)
    across = next_reset_at(before, timezone="America/New_York", settings=config)

    assert across - before == datetime.timedelta(hours=25)
    assert local_reading(across, "America/New_York") == "2026-11-02 00:00"


def test_resets_stay_one_local_day_apart_across_a_transition() -> None:
    """Walked forward day by day, every reset reads as local midnight — which is
    the property that actually matters, whatever the elapsed hours were."""
    config = settings()
    moment = utc(2026, 9, 1, 12, 0)

    for _ in range(10):
        moment = next_reset_at(moment, timezone="America/Santiago", settings=config)
        hour = moment.astimezone(ZoneInfo("America/Santiago")).hour
        assert hour in (0, 1), f"reset landed at local {hour}:00"


# ------------------------------------------------------------------ bad input


def test_an_unknown_timezone_is_refused() -> None:
    """The column is free text, and a learner whose zone cannot be resolved
    would otherwise never have their allowance reset at all."""
    with pytest.raises(ValueError, match="unknown timezone"):
        next_reset_at(utc(2026, 9, 17), timezone="Asia/Phnom-Penh", settings=settings())


def test_an_empty_timezone_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        load_timezone("")


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_reset_at(
            datetime.datetime(2026, 9, 17, 3, 0), timezone=PHNOM_PENH, settings=settings()
        )


def test_the_default_timezone_from_the_schema_resolves() -> None:
    """user_profiles.timezone defaults to this, so it had better be valid."""
    assert load_timezone(PHNOM_PENH).key == PHNOM_PENH

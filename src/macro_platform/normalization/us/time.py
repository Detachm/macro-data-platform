from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Final
from zoneinfo import ZoneInfo

from macro_platform.normalization.common import TimezoneRequiredError
from macro_platform.normalization.us.errors import NonexistentLocalTimeError

NEW_YORK_TZ: Final = ZoneInfo("America/New_York")


def to_us_market_utc(value: datetime) -> datetime:
    _raise_for_invalid_aware_datetime(value)
    return value.astimezone(UTC)


def us_trading_date(value: datetime) -> date:
    return to_us_market_utc(value).astimezone(NEW_YORK_TZ).date()


def _raise_for_invalid_aware_datetime(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TimezoneRequiredError("datetime must include a timezone")

    round_tripped = value.astimezone(UTC).astimezone(value.tzinfo)
    if _wall_time_tuple(round_tripped) != _wall_time_tuple(value):
        raise NonexistentLocalTimeError("NONEXISTENT_LOCAL_TIME")


def _wall_time_tuple(value: datetime) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
    )

from __future__ import annotations

from datetime import UTC, datetime


class TimezoneRequiredError(ValueError):
    pass


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TimezoneRequiredError("datetime must include a timezone")
    return value.astimezone(UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)

"""Versioned US equity calendar snapshot for fixture-backed normalization.

The 2026 snapshot follows the NYSE/Nasdaq published holiday and early-close
calendars. Unsupported years fail closed instead of being guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final, Literal

from macro_platform.normalization.us.errors import (
    UsCalendarUnavailableError,
    UsMarketClosedError,
)
from macro_platform.normalization.us.time import NEW_YORK_TZ, to_us_market_utc

UsEquityCalendarStatus = Literal["regular", "early_close", "closed"]
US_EQUITY_CALENDAR_VERSION: Final = "XNYS-XNAS-2026-holidays-v1"
US_EQUITY_CALENDAR_SOURCE_URLS: Final = (
    "https://www.nasdaqtrader.com/trader.aspx?id=calendar",
    "https://www.nyse.com/markets/hours-calendars",
)

_SUPPORTED_YEARS: Final = frozenset({2026})
_US_EQUITY_CLOSED_DAYS: Final = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    }
)
_US_EQUITY_HALF_DAYS: Final = frozenset(
    {
        date(2026, 11, 27),
        date(2026, 12, 24),
    }
)


@dataclass(frozen=True)
class _UsEquitySessionWindow:
    trading_date: date
    open_at: datetime
    close_at: datetime
    timezone: str = "America/New_York"
    calendar_version: str = US_EQUITY_CALENDAR_VERSION
    early_close: bool = False


@dataclass(frozen=True)
class _UsEquityCalendarDay:
    trading_date: date
    status: UsEquityCalendarStatus
    timezone: str = "America/New_York"
    calendar_version: str = US_EQUITY_CALENDAR_VERSION


def us_equity_calendar_day(trading_date: date) -> _UsEquityCalendarDay:
    if trading_date.year not in _SUPPORTED_YEARS:
        raise UsCalendarUnavailableError(
            f"US equity calendar snapshot is unavailable for {trading_date.year}"
        )

    if trading_date.weekday() >= 5 or trading_date in _US_EQUITY_CLOSED_DAYS:
        status: UsEquityCalendarStatus = "closed"
    elif trading_date in _US_EQUITY_HALF_DAYS:
        status = "early_close"
    else:
        status = "regular"

    return _UsEquityCalendarDay(trading_date=trading_date, status=status)


def us_equity_session_window(trading_date: date) -> _UsEquitySessionWindow:
    calendar_day = us_equity_calendar_day(trading_date)
    if calendar_day.status == "closed":
        raise UsMarketClosedError(f"US equity market is closed on {trading_date.isoformat()}")

    early_close = calendar_day.status == "early_close"
    close_time = time(13, 0) if early_close else time(16, 0)
    local_open = datetime.combine(trading_date, time(9, 30), tzinfo=NEW_YORK_TZ)
    local_close = datetime.combine(trading_date, close_time, tzinfo=NEW_YORK_TZ)

    return _UsEquitySessionWindow(
        trading_date=trading_date,
        open_at=to_us_market_utc(local_open),
        close_at=to_us_market_utc(local_close),
        early_close=early_close,
    )

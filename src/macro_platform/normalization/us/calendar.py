"""Versioned US equity calendar snapshots for fixture-backed normalization.

The bundled snapshots follow the NYSE/Nasdaq published holiday and early-close
calendars for 2025 through 2027. Callers may inject an approved snapshot
registry for replay or a newly reviewed operating window; missing years fail
closed instead of being guessed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from types import MappingProxyType
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


@dataclass(frozen=True)
class UsEquityCalendarSnapshot:
    """An approved holiday and early-close snapshot for one trading year."""

    calendar_version: str
    closed_days: frozenset[date]
    early_close_days: frozenset[date]


US_EQUITY_CALENDAR_SNAPSHOTS: Final[Mapping[int, UsEquityCalendarSnapshot]] = MappingProxyType(
    {
        2025: UsEquityCalendarSnapshot(
            calendar_version="XNYS-XNAS-2025-holidays-v1",
            closed_days=frozenset(
                {
                    date(2025, 1, 1),
                    date(2025, 1, 20),
                    date(2025, 2, 17),
                    date(2025, 4, 18),
                    date(2025, 5, 26),
                    date(2025, 6, 19),
                    date(2025, 7, 4),
                    date(2025, 9, 1),
                    date(2025, 11, 27),
                    date(2025, 12, 25),
                }
            ),
            early_close_days=frozenset(
                {
                    date(2025, 7, 3),
                    date(2025, 11, 28),
                    date(2025, 12, 24),
                }
            ),
        ),
        2026: UsEquityCalendarSnapshot(
            calendar_version=US_EQUITY_CALENDAR_VERSION,
            closed_days=frozenset(
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
            ),
            early_close_days=frozenset(
                {
                    date(2026, 11, 27),
                    date(2026, 12, 24),
                }
            ),
        ),
        2027: UsEquityCalendarSnapshot(
            calendar_version="XNYS-XNAS-2027-holidays-v1",
            closed_days=frozenset(
                {
                    date(2027, 1, 1),
                    date(2027, 1, 18),
                    date(2027, 2, 15),
                    date(2027, 3, 26),
                    date(2027, 5, 31),
                    date(2027, 6, 18),
                    date(2027, 7, 5),
                    date(2027, 9, 6),
                    date(2027, 11, 25),
                    date(2027, 12, 24),
                }
            ),
            early_close_days=frozenset({date(2027, 11, 26)}),
        ),
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


def us_equity_calendar_day(
    trading_date: date,
    *,
    snapshots: Mapping[int, UsEquityCalendarSnapshot] = US_EQUITY_CALENDAR_SNAPSHOTS,
) -> _UsEquityCalendarDay:
    snapshot = snapshots.get(trading_date.year)
    if snapshot is None:
        raise UsCalendarUnavailableError(
            f"US equity calendar snapshot is unavailable for {trading_date.year}"
        )

    if trading_date.weekday() >= 5 or trading_date in snapshot.closed_days:
        status: UsEquityCalendarStatus = "closed"
    elif trading_date in snapshot.early_close_days:
        status = "early_close"
    else:
        status = "regular"

    return _UsEquityCalendarDay(
        trading_date=trading_date,
        status=status,
        calendar_version=snapshot.calendar_version,
    )


def us_equity_session_window(
    trading_date: date,
    *,
    snapshots: Mapping[int, UsEquityCalendarSnapshot] = US_EQUITY_CALENDAR_SNAPSHOTS,
) -> _UsEquitySessionWindow:
    calendar_day = us_equity_calendar_day(trading_date, snapshots=snapshots)
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
        calendar_version=calendar_day.calendar_version,
        early_close=early_close,
    )

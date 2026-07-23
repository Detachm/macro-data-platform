"""US-specific normalization helpers.

These helpers normalize provider quirks before provider rows are mapped into the
public contracts. They are not public API DTO definitions.
"""

from macro_platform.normalization.us.calendar import (
    US_EQUITY_CALENDAR_VERSION,
    us_equity_calendar_day,
    us_equity_session_window,
)
from macro_platform.normalization.us.errors import (
    AmbiguousSymbolAliasError,
    NonexistentLocalTimeError,
    SymbolNormalizationError,
    UnitNormalizationError,
    UnsupportedExchangeError,
    UsCalendarUnavailableError,
    UsMarketClosedError,
    UsNormalizationError,
)
from macro_platform.normalization.us.symbols import (
    normalize_us_alias,
    normalize_us_symbol,
    resolve_us_alias_for_date,
    us_instrument_id,
    validate_us_aliases,
)
from macro_platform.normalization.us.time import to_us_market_utc, us_trading_date
from macro_platform.normalization.us.units import normalize_us_value

__all__ = [
    "AmbiguousSymbolAliasError",
    "NonexistentLocalTimeError",
    "SymbolNormalizationError",
    "US_EQUITY_CALENDAR_VERSION",
    "UnitNormalizationError",
    "UnsupportedExchangeError",
    "UsCalendarUnavailableError",
    "UsMarketClosedError",
    "UsNormalizationError",
    "normalize_us_alias",
    "normalize_us_symbol",
    "normalize_us_value",
    "resolve_us_alias_for_date",
    "to_us_market_utc",
    "us_equity_calendar_day",
    "us_equity_session_window",
    "us_instrument_id",
    "us_trading_date",
    "validate_us_aliases",
]

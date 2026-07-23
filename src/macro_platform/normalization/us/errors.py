from __future__ import annotations


class UsNormalizationError(ValueError):
    """Base class for US normalization failures."""

    code: str = "US_NORMALIZATION_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(f"{self.code}: {message}")


class SymbolNormalizationError(UsNormalizationError):
    code = "SYMBOL_UNRESOLVED"


class UnsupportedExchangeError(SymbolNormalizationError):
    code = "UNSUPPORTED_EXCHANGE"


class AmbiguousSymbolAliasError(SymbolNormalizationError):
    code = "AMBIGUOUS_SYMBOL_ALIAS"


class NonexistentLocalTimeError(UsNormalizationError):
    code = "NONEXISTENT_LOCAL_TIME"


class UsMarketClosedError(UsNormalizationError):
    code = "MARKET_CLOSED"


class UsCalendarUnavailableError(UsNormalizationError):
    code = "CALENDAR_UNAVAILABLE"


class UnitNormalizationError(UsNormalizationError):
    code = "UNIT_NORMALIZATION_ERROR"

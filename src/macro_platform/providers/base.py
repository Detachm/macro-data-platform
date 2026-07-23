from __future__ import annotations

from typing import Protocol

from macro_platform.contracts.macro import (
    MacroObservation,
    MacroObservationQuery,
    MacroRelease,
    MacroReleaseQuery,
    MacroSeries,
    MacroSeriesQuery,
)
from macro_platform.contracts.market import (
    BarQuery,
    Instrument,
    InstrumentQuery,
    MarketBar,
    MarketObservation,
    MarketObservationQuery,
)
from macro_platform.contracts.news import NewsEvent, NewsQuery
from macro_platform.contracts.provider import (
    FetchContext,
    ProviderCapabilities,
    ProviderHealth,
    ProviderPage,
)


class ProviderError(RuntimeError):
    default_code = "PROVIDER_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or self.default_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class ProviderAuthenticationError(ProviderError):
    default_code = "PROVIDER_AUTHENTICATION_FAILED"


class ProviderAuthorizationError(ProviderError):
    default_code = "PROVIDER_FORBIDDEN"


class ProviderRateLimitError(ProviderError):
    default_code = "PROVIDER_RATE_LIMITED"


class ProviderUnavailableError(ProviderError):
    default_code = "PROVIDER_UNAVAILABLE"


class ProviderTimeoutError(ProviderError):
    default_code = "PROVIDER_TIMEOUT"


class ProviderSchemaError(ProviderError):
    default_code = "PROVIDER_SCHEMA_CHANGED"


class ProviderCursorError(ProviderError):
    default_code = "PROVIDER_CURSOR_INVALID"


class UnsupportedCapabilityError(ProviderError):
    default_code = "UNSUPPORTED_CAPABILITY"


class BaseProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    async def healthcheck(self) -> ProviderHealth: ...

    async def aclose(self) -> None: ...


class MarketDataProvider(BaseProvider, Protocol):
    async def fetch_instruments(
        self, query: InstrumentQuery, context: FetchContext
    ) -> ProviderPage[Instrument]: ...

    async def fetch_bars(
        self, query: BarQuery, context: FetchContext
    ) -> ProviderPage[MarketBar]: ...

    async def fetch_market_observations(
        self, query: MarketObservationQuery, context: FetchContext
    ) -> ProviderPage[MarketObservation]: ...


class MacroDataProvider(BaseProvider, Protocol):
    async def fetch_macro_series(
        self, query: MacroSeriesQuery, context: FetchContext
    ) -> ProviderPage[MacroSeries]: ...

    async def fetch_macro_observations(
        self, query: MacroObservationQuery, context: FetchContext
    ) -> ProviderPage[MacroObservation]: ...

    async def fetch_macro_releases(
        self, query: MacroReleaseQuery, context: FetchContext
    ) -> ProviderPage[MacroRelease]: ...


class NewsProvider(BaseProvider, Protocol):
    async def fetch_news(
        self, query: NewsQuery, context: FetchContext
    ) -> ProviderPage[NewsEvent]: ...

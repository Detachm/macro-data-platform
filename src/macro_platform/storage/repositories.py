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
    MarketSnapshot,
    MarketSnapshotQuery,
)
from macro_platform.contracts.news import NewsEvent, NewsQuery


class DataRepository(Protocol):
    async def list_instruments(self, query: InstrumentQuery) -> list[Instrument]: ...

    async def list_bars(self, query: BarQuery) -> list[MarketBar]: ...

    async def list_snapshots(self, query: MarketSnapshotQuery) -> list[MarketSnapshot]: ...

    async def list_market_observations(
        self, query: MarketObservationQuery
    ) -> list[MarketObservation]: ...

    async def list_macro_series(self, query: MacroSeriesQuery) -> list[MacroSeries]: ...

    async def list_macro_observations(
        self, query: MacroObservationQuery
    ) -> list[MacroObservation]: ...

    async def list_macro_releases(self, query: MacroReleaseQuery) -> list[MacroRelease]: ...

    async def list_news(self, query: NewsQuery) -> list[NewsEvent]: ...


class EmptyDataRepository:
    """Development scaffold. Replace with PostgreSQL repositories dataset by dataset."""

    async def list_instruments(self, query: InstrumentQuery) -> list[Instrument]:
        return []

    async def list_bars(self, query: BarQuery) -> list[MarketBar]:
        return []

    async def list_snapshots(self, query: MarketSnapshotQuery) -> list[MarketSnapshot]:
        return []

    async def list_market_observations(
        self, query: MarketObservationQuery
    ) -> list[MarketObservation]:
        return []

    async def list_macro_series(self, query: MacroSeriesQuery) -> list[MacroSeries]:
        return []

    async def list_macro_observations(self, query: MacroObservationQuery) -> list[MacroObservation]:
        return []

    async def list_macro_releases(self, query: MacroReleaseQuery) -> list[MacroRelease]:
        return []

    async def list_news(self, query: NewsQuery) -> list[NewsEvent]:
        return []

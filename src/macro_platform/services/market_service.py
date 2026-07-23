from __future__ import annotations

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
from macro_platform.storage.repositories import DataRepository


class MarketService:
    def __init__(self, repository: DataRepository) -> None:
        self._repository = repository

    async def instruments(self, query: InstrumentQuery) -> list[Instrument]:
        return await self._repository.list_instruments(query)

    async def bars(self, query: BarQuery) -> list[MarketBar]:
        return await self._repository.list_bars(query)

    async def snapshots(self, query: MarketSnapshotQuery) -> list[MarketSnapshot]:
        return await self._repository.list_snapshots(query)

    async def observations(self, query: MarketObservationQuery) -> list[MarketObservation]:
        return await self._repository.list_market_observations(query)

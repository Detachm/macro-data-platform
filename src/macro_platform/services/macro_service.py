from __future__ import annotations

from macro_platform.contracts.macro import (
    MacroObservation,
    MacroObservationQuery,
    MacroRelease,
    MacroReleaseQuery,
    MacroSeries,
    MacroSeriesQuery,
)
from macro_platform.storage.repositories import DataRepository


class MacroService:
    def __init__(self, repository: DataRepository) -> None:
        self._repository = repository

    async def series(self, query: MacroSeriesQuery) -> list[MacroSeries]:
        return await self._repository.list_macro_series(query)

    async def observations(self, query: MacroObservationQuery) -> list[MacroObservation]:
        return await self._repository.list_macro_observations(query)

    async def releases(self, query: MacroReleaseQuery) -> list[MacroRelease]:
        return await self._repository.list_macro_releases(query)

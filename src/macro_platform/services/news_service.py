from __future__ import annotations

from macro_platform.contracts.news import NewsEvent, NewsQuery
from macro_platform.storage.repositories import DataRepository


class NewsService:
    def __init__(self, repository: DataRepository) -> None:
        self._repository = repository

    async def events(self, query: NewsQuery) -> list[NewsEvent]:
        return await self._repository.list_news(query)

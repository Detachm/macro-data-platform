from __future__ import annotations

from macro_platform.contracts.common import Region
from macro_platform.contracts.news import ContentMode, NewsEvent, NewsQuery
from macro_platform.services.news_service import NewsService
from macro_platform.storage.repositories import EmptyDataRepository
from tests.helpers import NOW, news_event


class NewsRepository(EmptyDataRepository):
    def __init__(self, events: list[NewsEvent]) -> None:
        self.events = events

    async def list_news(self, query: NewsQuery) -> list[NewsEvent]:
        return self.events


def query() -> NewsQuery:
    return NewsQuery(
        regions={Region.CN},
        published_from=NOW.replace(day=22),
        published_to=NOW.replace(day=24),
        as_of=NOW,
        content_mode=ContentMode.FULL_TEXT,
    )


async def test_news_service_does_not_filter_or_redact_legacy_rights_flags() -> None:
    payload = news_event(external_llm_allowed=False).model_dump()
    payload["body"] = "full article"
    payload["content_mode"] = ContentMode.FULL_TEXT
    payload["usage_rights"]["storage_allowed"] = False
    event = NewsEvent.model_validate(payload)

    assert await NewsService(NewsRepository([event])).events(query()) == [event]

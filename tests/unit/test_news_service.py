from __future__ import annotations

from macro_platform.contracts.common import Region
from macro_platform.contracts.news import ContentMode, NewsQuery
from macro_platform.services.news_service import NewsService
from macro_platform.storage.repositories import EmptyDataRepository
from tests.helpers import NOW, news_event


class NewsRepository(EmptyDataRepository):
    def __init__(self, events: list) -> None:
        self.events = events

    async def list_news(self, query: NewsQuery) -> list:
        return self.events


def query(mode: ContentMode) -> NewsQuery:
    return NewsQuery(
        regions={Region.CN},
        published_from=NOW.replace(day=22),
        published_to=NOW.replace(day=24),
        as_of=NOW,
        content_mode=mode,
    )


async def test_news_017_external_llm_receives_only_authorized_content() -> None:
    allowed = news_event()
    denied = news_event(external_llm_allowed=False).model_copy(update={"news_id": "denied"})
    service = NewsService(NewsRepository([allowed, denied]))

    events = await service.events(query(ContentMode.SNIPPET), for_external_llm=True)

    assert events[0].summary is not None
    assert events[0].body is None
    assert events[1].content_mode is ContentMode.HEADLINE
    assert events[1].summary is None
    assert events[1].body is None


async def test_internal_query_preserves_original_event() -> None:
    original = news_event()
    service = NewsService(NewsRepository([original]))
    assert await service.events(query(ContentMode.SNIPPET)) == [original]

from __future__ import annotations

from macro_platform.contracts.news import ContentMode, NewsEvent, NewsQuery
from macro_platform.contracts.provider import Dataset
from macro_platform.governance.source_policy import (
    NonProductionSourcePolicy,
    PolicyPurpose,
    SourcePolicy,
)
from macro_platform.storage.repositories import DataRepository


class NewsService:
    def __init__(
        self,
        repository: DataRepository,
        *,
        source_policy: SourcePolicy | None = None,
    ) -> None:
        self._repository = repository
        self._source_policy = source_policy or NonProductionSourcePolicy()

    async def events(self, query: NewsQuery, *, for_external_llm: bool = False) -> list[NewsEvent]:
        events = await self._repository.list_news(query)
        if not for_external_llm:
            return events

        sanitized: list[NewsEvent] = []
        for event in events:
            if not all(
                self._source_policy.decision(
                    provider_id=event.source.provider_id,
                    dataset=Dataset.NEWS,
                    region=region,
                    purpose=PolicyPurpose.EXTERNAL_LLM,
                ).allowed
                for region in event.regions
            ):
                continue
            update: dict[str, object] = {}
            if (
                not event.usage_rights.external_llm_allowed
                or query.content_mode is ContentMode.HEADLINE
            ):
                update.update(summary=None, body=None, content_mode=ContentMode.HEADLINE)
            elif query.content_mode is ContentMode.SNIPPET:
                update.update(body=None, content_mode=ContentMode.SNIPPET)
            sanitized.append(event.model_copy(update=update) if update else event)
        return sanitized

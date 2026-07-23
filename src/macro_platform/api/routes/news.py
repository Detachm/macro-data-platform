from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from macro_platform.api.dependencies import RepositoryDep, RequestIdDep
from macro_platform.api.responses import item_list_response
from macro_platform.contracts.common import ItemList, Region, SuccessEnvelope
from macro_platform.contracts.news import ContentMode, NewsEvent, NewsQuery, SourceTier
from macro_platform.normalization.common import utc_now
from macro_platform.services.news_service import NewsService

router = APIRouter(prefix="/v1/news", tags=["news"])


@router.get("", response_model=SuccessEnvelope[ItemList[NewsEvent]])
async def events(
    repository: RepositoryDep,
    request_id: RequestIdDep,
    regions: Annotated[list[Region], Query(min_length=1)],
    published_from: datetime,
    published_to: datetime,
    as_of: datetime | None = None,
    entity_ids: Annotated[list[str] | None, Query(alias="entity_id")] = None,
    topics: Annotated[list[str] | None, Query(alias="topic")] = None,
    languages: Annotated[list[str] | None, Query(alias="language")] = None,
    source_tiers: Annotated[list[SourceTier] | None, Query(alias="source_tier")] = None,
    include_superseded: bool = False,
    content_mode: ContentMode = ContentMode.SNIPPET,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> SuccessEnvelope[ItemList[NewsEvent]]:
    effective_as_of = as_of or utc_now()
    query = NewsQuery(
        regions=set(regions),
        published_from=published_from,
        published_to=published_to,
        as_of=effective_as_of,
        entity_ids=entity_ids or [],
        topics=topics or [],
        languages=set(languages or []),
        source_tiers=set(source_tiers or []),
        include_superseded=include_superseded,
        content_mode=content_mode,
        cursor=cursor,
        limit=limit,
    )
    items = await NewsService(repository).events(query)
    return item_list_response(
        request_id=request_id,
        as_of=effective_as_of,
        snapshot_at=utc_now(),
        items=items,
        limit=limit,
    )

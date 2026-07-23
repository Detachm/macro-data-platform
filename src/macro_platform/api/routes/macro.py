from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from macro_platform.api.dependencies import RepositoryDep, RequestIdDep
from macro_platform.api.responses import item_list_response
from macro_platform.contracts.common import ItemList, Region, SuccessEnvelope
from macro_platform.contracts.macro import (
    MacroObservation,
    MacroObservationQuery,
    MacroRelease,
    MacroReleaseQuery,
    MacroSeries,
    MacroSeriesQuery,
    RevisionPolicy,
)
from macro_platform.normalization.common import utc_now
from macro_platform.services.macro_service import MacroService

router = APIRouter(prefix="/v1/macro", tags=["macro"])


@router.get("/series", response_model=SuccessEnvelope[ItemList[MacroSeries]])
async def series(
    repository: RepositoryDep,
    request_id: RequestIdDep,
    regions: Annotated[list[Region], Query(min_length=1)],
    series_ids: Annotated[list[str] | None, Query(alias="series_id")] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> SuccessEnvelope[ItemList[MacroSeries]]:
    query = MacroSeriesQuery(
        regions=set(regions), series_ids=series_ids or [], cursor=cursor, limit=limit
    )
    items = await MacroService(repository).series(query)
    now = utc_now()
    return item_list_response(
        request_id=request_id,
        as_of=now,
        snapshot_at=now,
        items=items,
        limit=limit,
    )


@router.get("/observations", response_model=SuccessEnvelope[ItemList[MacroObservation]])
async def observations(
    repository: RepositoryDep,
    request_id: RequestIdDep,
    series_ids: Annotated[list[str], Query(alias="series_id", min_length=1)],
    period_from: date,
    period_to: date,
    as_of: datetime | None = None,
    revision_policy: RevisionPolicy = RevisionPolicy.LATEST_AS_OF,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> SuccessEnvelope[ItemList[MacroObservation]]:
    effective_as_of = as_of or utc_now()
    query = MacroObservationQuery(
        series_ids=series_ids,
        period_from=period_from,
        period_to=period_to,
        as_of=effective_as_of,
        revision_policy=revision_policy,
        cursor=cursor,
        limit=limit,
    )
    items = await MacroService(repository).observations(query)
    return item_list_response(
        request_id=request_id,
        as_of=effective_as_of,
        snapshot_at=utc_now(),
        items=items,
        limit=limit,
    )


@router.get("/releases", response_model=SuccessEnvelope[ItemList[MacroRelease]])
async def releases(
    repository: RepositoryDep,
    request_id: RequestIdDep,
    regions: Annotated[list[Region], Query(min_length=1)],
    scheduled_from: datetime,
    scheduled_to: datetime,
    as_of: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> SuccessEnvelope[ItemList[MacroRelease]]:
    effective_as_of = as_of or utc_now()
    query = MacroReleaseQuery(
        regions=set(regions),
        scheduled_from=scheduled_from,
        scheduled_to=scheduled_to,
        as_of=effective_as_of,
        cursor=cursor,
        limit=limit,
    )
    items = await MacroService(repository).releases(query)
    return item_list_response(
        request_id=request_id,
        as_of=effective_as_of,
        snapshot_at=utc_now(),
        items=items,
        limit=limit,
    )

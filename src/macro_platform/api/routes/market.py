from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from macro_platform.api.dependencies import RepositoryDep, RequestIdDep
from macro_platform.api.responses import item_list_response
from macro_platform.contracts.common import ItemList, Region, SuccessEnvelope
from macro_platform.contracts.market import (
    Adjustment,
    BarQuery,
    Interval,
    MarketBar,
    MarketObservation,
    MarketObservationQuery,
    MarketSnapshot,
    MarketSnapshotQuery,
)
from macro_platform.normalization.common import utc_now
from macro_platform.services.market_service import MarketService

router = APIRouter(prefix="/v1/market", tags=["market"])


@router.get("/bars", response_model=SuccessEnvelope[ItemList[MarketBar]])
async def bars(
    repository: RepositoryDep,
    request_id: RequestIdDep,
    instrument_ids: Annotated[list[str], Query(alias="instrument_id", min_length=1)],
    interval: Interval,
    start: datetime,
    end: datetime,
    adjustment: Adjustment = Adjustment.RAW,
    as_of: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> SuccessEnvelope[ItemList[MarketBar]]:
    effective_as_of = as_of or utc_now()
    query = BarQuery(
        instrument_ids=instrument_ids,
        interval=interval,
        start=start,
        end=end,
        adjustment=adjustment,
        as_of=effective_as_of,
        cursor=cursor,
        limit=limit,
    )
    items = await MarketService(repository).bars(query)
    return item_list_response(
        request_id=request_id,
        as_of=effective_as_of,
        snapshot_at=utc_now(),
        items=items,
        limit=limit,
    )


@router.get("/snapshots", response_model=SuccessEnvelope[ItemList[MarketSnapshot]])
async def snapshots(
    repository: RepositoryDep,
    request_id: RequestIdDep,
    instrument_ids: Annotated[list[str], Query(alias="instrument_id", min_length=1)],
    as_of: datetime | None = None,
) -> SuccessEnvelope[ItemList[MarketSnapshot]]:
    effective_as_of = as_of or utc_now()
    query = MarketSnapshotQuery(instrument_ids=instrument_ids, as_of=effective_as_of)
    items = await MarketService(repository).snapshots(query)
    return item_list_response(
        request_id=request_id,
        as_of=effective_as_of,
        snapshot_at=utc_now(),
        items=items,
        limit=len(instrument_ids),
    )


@router.get("/observations", response_model=SuccessEnvelope[ItemList[MarketObservation]])
async def observations(
    repository: RepositoryDep,
    request_id: RequestIdDep,
    regions: Annotated[list[Region], Query(min_length=1)],
    metric_codes: Annotated[list[str], Query(alias="metric_code", min_length=1)],
    start: datetime,
    end: datetime,
    scope_ids: Annotated[list[str] | None, Query(alias="scope_id")] = None,
    as_of: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> SuccessEnvelope[ItemList[MarketObservation]]:
    effective_as_of = as_of or utc_now()
    query = MarketObservationQuery(
        regions=set(regions),
        metric_codes=metric_codes,
        scope_ids=scope_ids or [],
        start=start,
        end=end,
        as_of=effective_as_of,
        cursor=cursor,
        limit=limit,
    )
    items = await MarketService(repository).observations(query)
    return item_list_response(
        request_id=request_id,
        as_of=effective_as_of,
        snapshot_at=utc_now(),
        items=items,
        limit=limit,
    )

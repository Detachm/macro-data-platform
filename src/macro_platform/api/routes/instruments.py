from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from macro_platform.api.dependencies import RepositoryDep, RequestIdDep
from macro_platform.api.responses import item_list_response
from macro_platform.contracts.common import AssetClass, ItemList, Region, SuccessEnvelope
from macro_platform.contracts.market import Instrument, InstrumentQuery
from macro_platform.normalization.common import utc_now
from macro_platform.services.market_service import MarketService

router = APIRouter(prefix="/v1/instruments", tags=["instruments"])


@router.get("", response_model=SuccessEnvelope[ItemList[Instrument]])
async def list_instruments(
    repository: RepositoryDep,
    request_id: RequestIdDep,
    regions: Annotated[list[Region], Query(min_length=1)],
    venues: Annotated[list[str] | None, Query()] = None,
    asset_classes: Annotated[list[AssetClass] | None, Query()] = None,
    active_on: date | None = None,
    modified_since: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> SuccessEnvelope[ItemList[Instrument]]:
    query = InstrumentQuery(
        regions=set(regions),
        venues=set(venues or []),
        asset_classes=set(asset_classes or []),
        active_on=active_on,
        modified_since=modified_since,
        cursor=cursor,
        limit=limit,
    )
    items = await MarketService(repository).instruments(query)
    now = utc_now()
    return item_list_response(
        request_id=request_id,
        as_of=now,
        snapshot_at=now,
        items=items,
        limit=limit,
    )

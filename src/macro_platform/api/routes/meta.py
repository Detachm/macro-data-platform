from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from macro_platform.api.dependencies import RegistryDep, RequestIdDep
from macro_platform.contracts.common import ItemList, Region, SuccessEnvelope
from macro_platform.contracts.provider import ProviderCapabilities
from macro_platform.normalization.common import utc_now

router = APIRouter(prefix="/v1/meta", tags=["meta"])


@router.get("/capabilities", response_model=SuccessEnvelope[ItemList[ProviderCapabilities]])
async def capabilities(
    registry: RegistryDep,
    request_id: RequestIdDep,
    regions: Annotated[list[Region] | None, Query()] = None,
) -> SuccessEnvelope[ItemList[ProviderCapabilities]]:
    now = utc_now()
    requested = set(regions or [])
    items = [
        capability
        for capability in registry.capabilities()
        if not requested or capability.regions.intersection(requested)
    ]
    return SuccessEnvelope[ItemList[ProviderCapabilities]](
        request_id=request_id,
        as_of=now,
        snapshot_at=now,
        data=ItemList[ProviderCapabilities](items=items),
    )

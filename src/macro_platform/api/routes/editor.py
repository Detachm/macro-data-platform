from __future__ import annotations

from fastapi import APIRouter

from macro_platform.api.dependencies import RepositoryDep, RequestIdDep
from macro_platform.contracts.common import SuccessEnvelope
from macro_platform.contracts.editor import EditorContext, EditorContextRequest
from macro_platform.normalization.common import utc_now
from macro_platform.services.editor_context_service import EditorContextService
from macro_platform.services.macro_service import MacroService
from macro_platform.services.market_service import MarketService
from macro_platform.services.news_service import NewsService

router = APIRouter(prefix="/v1/editor", tags=["editor"])


@router.post("/context", response_model=SuccessEnvelope[EditorContext])
async def build_context(
    body: EditorContextRequest,
    repository: RepositoryDep,
    request_id: RequestIdDep,
) -> SuccessEnvelope[EditorContext]:
    service = EditorContextService(
        MarketService(repository),
        MacroService(repository),
        NewsService(repository),
    )
    context = await service.build(body)
    return SuccessEnvelope[EditorContext](
        request_id=request_id,
        as_of=context.as_of,
        snapshot_at=utc_now(),
        data=context,
    )

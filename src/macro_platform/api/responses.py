from __future__ import annotations

from datetime import datetime
from uuid import UUID

from macro_platform.contracts.common import ItemList, PageMeta, StrictModel, SuccessEnvelope


def item_list_response[T: StrictModel](
    *,
    request_id: UUID,
    as_of: datetime,
    snapshot_at: datetime,
    items: list[T],
    limit: int,
    next_cursor: str | None = None,
) -> SuccessEnvelope[ItemList[T]]:
    return SuccessEnvelope[ItemList[T]](
        request_id=request_id,
        as_of=as_of,
        snapshot_at=snapshot_at,
        data=ItemList[T](items=items),
        page=PageMeta(limit=limit, has_more=next_cursor is not None, next_cursor=next_cursor),
    )

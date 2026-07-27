from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from macro_platform.contracts.provider import Dataset
from macro_platform.providers.base import UnsupportedCapabilityError
from macro_platform.storage.database import Database
from macro_platform.storage.repositories import IngestionCheckpointRepository


@dataclass(frozen=True)
class CommittedPage:
    provider_role: str
    dataset: Dataset
    region: str
    page_fingerprint: str
    source_watermark: str | None
    next_cursor: str | None
    accepted_record_ids: list[str]


class IngestionCheckpointService:
    """Durable page idempotency, audit evidence, and watermark recovery seam."""

    async def reject_unsupported_historical_pit(
        self,
        database: Database,
        *,
        run_id: UUID,
        provider_id: str,
        supports_point_in_time: bool,
        historical_request: bool,
    ) -> None:
        if not historical_request or supports_point_in_time:
            return
        async with database.session() as audit_session, audit_session.begin():
            IngestionCheckpointRepository(audit_session, ingestion_run_id=run_id).add_audit(
                run_id=run_id,
                provider_id=provider_id,
                audit_kind="unsupported_historical_pit",
                payload={"supports_point_in_time": False},
            )
        raise UnsupportedCapabilityError(f"{provider_id} does not support historical point-in-time")

    async def record_raw_timestamp(
        self,
        database: Database,
        *,
        run_id: UUID,
        provider_id: str,
        raw_value: str,
        raw_timezone: str,
        normalized_utc: str,
    ) -> None:
        async with database.session() as audit_session, audit_session.begin():
            IngestionCheckpointRepository(audit_session, ingestion_run_id=run_id).add_audit(
                run_id=run_id,
                provider_id=provider_id,
                audit_kind="raw_timestamp_normalization",
                payload={
                    "raw_value": raw_value,
                    "raw_timezone": raw_timezone,
                    "normalized_utc": normalized_utc,
                },
            )

    async def record_rejection(
        self,
        database: Database,
        *,
        run_id: UUID,
        provider_id: str,
        error_code: str,
        redacted_payload: dict[str, object],
    ) -> None:
        async with database.session() as rejection_session, rejection_session.begin():
            IngestionCheckpointRepository(rejection_session, ingestion_run_id=run_id).add_rejection(
                run_id=run_id,
                provider_id=provider_id,
                error_code=error_code,
                redacted_payload=redacted_payload,
            )

    async def commit_page(
        self,
        repository: IngestionCheckpointRepository,
        page: CommittedPage,
        write_records: Callable[[AsyncSession], Awaitable[None]],
    ) -> bool:
        if not await repository.reserve_page(
            provider_role=page.provider_role,
            dataset=page.dataset,
            region=page.region,
            page_fingerprint=page.page_fingerprint,
            source_watermark=page.source_watermark,
            next_cursor=page.next_cursor,
            accepted_record_ids=page.accepted_record_ids,
        ):
            return False
        await write_records(repository.session)
        await repository.save_watermark(
            provider_role=page.provider_role,
            dataset=page.dataset,
            region=page.region,
            watermark=page.source_watermark,
            cursor=page.next_cursor,
        )
        return True

    async def recover_committed_watermark(
        self,
        repository: IngestionCheckpointRepository,
        *,
        provider_role: str,
        dataset: Dataset,
        region: str,
    ) -> tuple[str | None, str | None]:
        return await repository.load_watermark(
            provider_role=provider_role, dataset=dataset, region=region
        )

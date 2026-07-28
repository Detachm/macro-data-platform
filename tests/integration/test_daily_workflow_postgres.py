from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from alembic import command
from alembic.config import Config
from docker.errors import DockerException
from pydantic import SecretStr
from sqlalchemy import func, select
from testcontainers.postgres import PostgresContainer

from macro_platform.config import Settings
from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset
from macro_platform.jobs.scheduler import (
    PostgresReportDateLock,
    ScheduledIngestionWorker,
    ScheduledTaskResult,
)
from macro_platform.services.daily_workflow import (
    DailyReportWorkflow,
    PostgresReportGenerationStore,
    build_generation_service,
)
from macro_platform.services.delivery_recovery import (
    DeliveryRecoveryService,
    PostgresDeliveryRecoveryAuditStore,
)
from macro_platform.services.llm import LlmError
from macro_platform.services.report_delivery import (
    ConfiguredFeishuDelivery,
    PostgresReportDeliveryStore,
)
from macro_platform.services.report_input_materializer import (
    MaterializedReportInput,
    PostgresReportInputSnapshotStore,
)
from macro_platform.services.report_input_quality import (
    REQUIRED_REPORT_INPUT_IDS,
    ReportInputQualityGate,
)
from macro_platform.services.workflow_alerts import (
    ConfiguredFeishuAlerts,
    PostgresWorkflowAlertStore,
)
from macro_platform.services.workflow_operations import (
    PostgresWorkerReadinessReader,
    PostgresWorkflowOperationsReader,
)
from macro_platform.storage.database import Database
from macro_platform.storage.models import (
    DailyReportRow,
    DeliveryAttemptRow,
    DeliveryOperatorActionRow,
    WorkflowAlertAttemptRow,
)
from macro_platform.storage.reporting import ReportInputSnapshot

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
REPORT_DATE = date(2026, 7, 23)
NOW = datetime(2026, 7, 23, 0, 30, tzinfo=UTC)


@pytest.fixture(scope="module")
def postgresql_url() -> Iterator[str]:
    supplied_url = os.environ.get("CONTRACT_TEST_DATABASE_URL")
    if supplied_url is not None:
        yield supplied_url
        return
    try:
        with PostgresContainer(
            "postgres:16-alpine",
            username="macro",
            password="macro",
            dbname="macro_workflow_test",
        ) as postgres:
            yield postgres.get_connection_url().replace("psycopg2", "asyncpg")
    except DockerException as error:
        pytest.skip(f"Docker is unavailable for daily workflow PostgreSQL E2E: {error}")


@pytest.fixture(scope="module")
def migrated_postgresql_url(postgresql_url: str) -> Iterator[str]:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = postgresql_url
    command.upgrade(config, "head")
    yield postgresql_url


@pytest.fixture
async def database(migrated_postgresql_url: str) -> AsyncIterator[Database]:
    value = Database(migrated_postgresql_url)
    try:
        assert await value.ready()
        yield value
    finally:
        await value.dispose()


def _snapshot() -> ReportInputSnapshot:
    report = json.loads(
        (ROOT / "tests/golden/daily_report_v1_success.json").read_text(encoding="utf-8")
    )
    source_references = report["sections"]["source_references"]["items"]
    source_ref_ids = [item["source_ref_id"] for item in source_references]
    input_snapshot = report["input_snapshot"]
    facts = [
        {
            "fact_id": fact_id,
            "input_id": "market.cn.core_indices.previous_close",
            "fact_type": "verified_display",
            "section_id": "executive_summary",
            "label": f"已验证事实 {index}",
            "display_text": f"已验证事实 {index}",
            "value": None,
            "available_at": input_snapshot["cutoff_at"],
            "report_date": report["report_date"],
            "source_ref_ids": source_ref_ids,
        }
        for index, fact_id in enumerate(input_snapshot["fact_ids"], start=1)
    ]
    payload = {
        **input_snapshot,
        "facts": facts,
        "source_ref_ids": source_ref_ids,
        "source_references": source_references,
        "input_quality": {
            input_id: {"status": "available", "required": True, "reason": None}
            for input_id in REQUIRED_REPORT_INPUT_IDS
        },
        "editor_context": {
            "facts": facts,
            "source_references": source_references,
        },
    }
    return ReportInputSnapshot(
        snapshot_id=input_snapshot["snapshot_id"],
        snapshot_version=input_snapshot["snapshot_version"],
        report_date=REPORT_DATE,
        as_of=datetime.fromisoformat(input_snapshot["as_of"].replace("Z", "+00:00")),
        cutoff_at=datetime.fromisoformat(input_snapshot["cutoff_at"].replace("Z", "+00:00")),
        fingerprint_sha256=input_snapshot["fingerprint_sha256"],
        fact_ids=input_snapshot["fact_ids"],
        payload=payload,
    )


class _ProviderTask:
    task_id = "mock.required-inputs"
    required = True

    async def run(self, report_date: date) -> ScheduledTaskResult:
        assert report_date == REPORT_DATE
        return ScheduledTaskResult(
            task_id=self.task_id,
            provider_role="mock.providers.complete",
            status="succeeded",
            dataset=Dataset.BARS,
            region=Region.CN,
            run_id=uuid4(),
        )


class _PersistedMaterializer:
    def __init__(self, database: Database, snapshot: ReportInputSnapshot) -> None:
        self._store = PostgresReportInputSnapshotStore(database)
        self._snapshot = snapshot

    async def materialize(
        self,
        report_date: date,
        *,
        task_results: tuple[ScheduledTaskResult, ...],
    ) -> MaterializedReportInput:
        assert report_date == REPORT_DATE
        assert task_results[0].run_id is not None
        await self._store.put(self._snapshot)
        return MaterializedReportInput(
            snapshot=self._snapshot,
            quality=ReportInputQualityGate().evaluate(self._snapshot),
        )


class _UnavailableLlm:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request: object) -> object:
        del request
        self.calls += 1
        raise LlmError("mocked LLM outage")


async def test_e2e_033_postgres_workflow_falls_back_and_delivers_once(
    database: Database,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "test-token", "expire": 7200},
            )
        return httpx.Response(
            200,
            json={"code": 0, "data": {"message_id": "om_workflow_e2e"}},
        )

    settings = Settings(
        _env_file=None,
        feishu_delivery_enabled=True,
        feishu_app_id="cli_test",
        feishu_app_secret=SecretStr("secret-test"),
        feishu_chat_id="oc_daily",
        feishu_alert_chat_id="oc_alert",
    )
    llm = _UnavailableLlm()
    generation_store = PostgresReportGenerationStore(database)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        configured_delivery = ConfiguredFeishuDelivery(
            settings=settings,
            client=client,
            store=PostgresReportDeliveryStore(database),
        )
        workflow = DailyReportWorkflow(
            generation_service=build_generation_service(
                llm=llm,  # type: ignore[arg-type]
                now=lambda: NOW,
                timeout_seconds=30,
                max_attempts=1,
            ),
            store=generation_store,
            report_delivery=configured_delivery,
            alert_delivery=ConfiguredFeishuAlerts(
                settings=settings,
                client=client,
                store=PostgresWorkflowAlertStore(database),
            ),
            model="mocked-llm",
            report_version="v1",
            timezone=ZoneInfo("Asia/Shanghai"),
            publish_hour=8,
            publish_minute=30,
            now=lambda: NOW,
        )
        worker = ScheduledIngestionWorker(
            tasks=[_ProviderTask()],
            report_date_lock=PostgresReportDateLock(database),
            input_materializer=_PersistedMaterializer(database, _snapshot()),
            report_workflow=workflow,
        )

        first = await worker.run_for_date(REPORT_DATE)
        replay = await worker.run_for_date(REPORT_DATE)
        assert first.report_id is not None
        recovery_service = DeliveryRecoveryService(
            delivery=configured_delivery,
            audit_store=PostgresDeliveryRecoveryAuditStore(database),
        )
        recovery_request_id = uuid4()
        recovered = await recovery_service.retry(
            request_id=recovery_request_id,
            report_id=first.report_id,
            confirmed_not_delivered=False,
        )
        recovery_replay = await recovery_service.retry(
            request_id=recovery_request_id,
            report_id=first.report_id,
            confirmed_not_delivered=False,
        )

    message_requests = [
        request for request in requests if request.url.path.endswith("/im/v1/messages")
    ]
    assert first.status == "degraded"
    assert replay.status == "degraded"
    assert first.workflow_run_id == replay.workflow_run_id
    assert first.report_id == replay.report_id
    assert first.delivery_status == replay.delivery_status == "succeeded"
    assert llm.calls == 1
    assert len(message_requests) == 1
    assert json.loads(message_requests[0].content)["receive_id"] == "oc_daily"
    assert recovered.status == recovery_replay.status == "succeeded"
    assert recovered.replayed is False
    assert recovery_replay.replayed is True

    async with database.session() as session:
        report_count = await session.scalar(
            select(func.count())
            .select_from(DailyReportRow)
            .where(DailyReportRow.report_id == first.report_id)
        )
        delivery_count = await session.scalar(
            select(func.count())
            .select_from(DeliveryAttemptRow)
            .where(DeliveryAttemptRow.report_id == first.report_id)
        )
        alert_count = await session.scalar(
            select(func.count())
            .select_from(WorkflowAlertAttemptRow)
            .where(WorkflowAlertAttemptRow.workflow_run_id == first.workflow_run_id)
        )
        operator_action_count = await session.scalar(
            select(func.count())
            .select_from(DeliveryOperatorActionRow)
            .where(DeliveryOperatorActionRow.request_id == recovery_request_id)
        )
    assert report_count == 1
    assert delivery_count == 1
    assert alert_count == 0
    assert operator_action_count == 1

    operations = await PostgresWorkflowOperationsReader(database).load(REPORT_DATE)
    assert operations.status == "delivered"
    assert operations.operator_attention_required is False
    assert [item.report_id for item in operations.reports] == [first.report_id]
    assert [item.status for item in operations.deliveries] == ["succeeded"]
    assert operations.snapshots[0].quality_status == "passed"
    assert [item.status for item in operations.operator_actions] == ["succeeded"]

    readiness_settings = Settings(
        _env_file=None,
        provider_mode="live",
        us_provider_mode="live",
        twelve_data_api_key=SecretStr("test-us-key"),
        twelve_data_cursor_secret=SecretStr("test-us-cursor"),
        feishu_delivery_enabled=True,
        feishu_app_id="cli_test",
        feishu_app_secret=SecretStr("secret-test"),
        feishu_chat_id="oc_daily",
        feishu_alert_chat_id="oc_alert",
    )
    readiness = await PostgresWorkerReadinessReader(database, readiness_settings).check()
    assert readiness.status == "ready"
    assert readiness.unmet_requirements == ()

    same_chat_readiness = await PostgresWorkerReadinessReader(
        database,
        readiness_settings.model_copy(update={"feishu_alert_chat_id": "oc_daily"}),
    ).check()
    assert same_chat_readiness.status == "not_ready"
    assert "FEISHU_CHATS_NOT_DISTINCT" in same_chat_readiness.unmet_requirements

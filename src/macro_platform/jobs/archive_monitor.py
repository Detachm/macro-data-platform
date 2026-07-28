"""Check the backup disk and send a durable, idempotent capacity alert."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import httpx

from macro_platform.config import Settings, get_settings
from macro_platform.observability import configure_logging
from macro_platform.services.workflow_alerts import (
    ConfiguredFeishuAlerts,
    PostgresWorkflowAlertStore,
    WorkflowAlert,
)
from macro_platform.storage.database import Database

ArchiveCapacityStatus = Literal["ok", "warning", "critical", "unavailable"]


class _AlertResult(Protocol):
    @property
    def status(self) -> str: ...


class _AlertDelivery(Protocol):
    async def deliver(self, alert: WorkflowAlert) -> _AlertResult: ...


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor the PostgreSQL backup filesystem")
    parser.add_argument("--path", type=Path, default=Path("/archive/backups"))
    parser.add_argument("--warning-percent", type=int, default=70)
    parser.add_argument("--critical-percent", type=int, default=85)
    parser.add_argument("--minimum-total-bytes", type=int, default=0)
    arguments = parser.parse_args()
    if not 1 <= arguments.warning_percent < arguments.critical_percent <= 100:
        parser.error("thresholds must satisfy 1 <= warning < critical <= 100")
    if arguments.minimum_total_bytes < 0:
        parser.error("--minimum-total-bytes must not be negative")
    settings = get_settings()
    configure_logging(settings.log_level)
    raise SystemExit(
        asyncio.run(
            _run_configured_monitor(
                settings=settings,
                path=arguments.path,
                warning_percent=arguments.warning_percent,
                critical_percent=arguments.critical_percent,
                minimum_total_bytes=arguments.minimum_total_bytes,
            )
        )
    )


async def _run_configured_monitor(
    *,
    settings: Settings,
    path: Path,
    warning_percent: int,
    critical_percent: int,
    minimum_total_bytes: int,
) -> int:
    database = Database(settings.database_url)
    try:
        async with httpx.AsyncClient() as client:
            delivery = ConfiguredFeishuAlerts(
                settings=settings,
                client=client,
                store=PostgresWorkflowAlertStore(database),
            )
            return await monitor_archive_capacity(
                path=path,
                warning_percent=warning_percent,
                critical_percent=critical_percent,
                minimum_total_bytes=minimum_total_bytes,
                delivery=delivery,
            )
    finally:
        await database.dispose()


async def monitor_archive_capacity(
    *,
    path: Path,
    warning_percent: int,
    critical_percent: int,
    delivery: _AlertDelivery,
    minimum_total_bytes: int = 0,
    now: datetime | None = None,
) -> int:
    report_date = (now or datetime.now(tz=ZoneInfo("Asia/Shanghai"))).date()
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        status: ArchiveCapacityStatus = "unavailable"
        used_percent: float | None = None
    else:
        used_percent = usage.used * 100 / usage.total if usage.total else 100.0
        if usage.total < minimum_total_bytes:
            status = "unavailable"
        elif used_percent >= critical_percent:
            status = "critical"
        elif used_percent >= warning_percent:
            status = "warning"
        else:
            status = "ok"

    output = {
        "event": "archive_capacity",
        "status": status,
        "used_percent": None if used_percent is None else round(used_percent, 2),
        "warning_percent": warning_percent,
        "critical_percent": critical_percent,
        "minimum_total_bytes": minimum_total_bytes,
    }
    if status == "ok":
        print(json.dumps(output, separators=(",", ":")))
        return 0

    alert = _build_alert(
        report_date=report_date,
        status=status,
        used_percent=used_percent,
        warning_percent=warning_percent,
        critical_percent=critical_percent,
    )
    result = await delivery.deliver(alert)
    output["alert_status"] = result.status
    print(json.dumps(output, separators=(",", ":")))
    if result.status != "succeeded":
        return 1
    return 1 if status in {"critical", "unavailable"} else 0


def _build_alert(
    *,
    report_date: date,
    status: ArchiveCapacityStatus,
    used_percent: float | None,
    warning_percent: int,
    critical_percent: int,
) -> WorkflowAlert:
    if status == "unavailable":
        error_code = "ARCHIVE_FILESYSTEM_UNAVAILABLE"
        summary = "PostgreSQL 备份文件系统不可访问或容量不符合独立归档盘预期。"
    else:
        triggered_threshold = critical_percent if status == "critical" else warning_percent
        error_code = (
            "ARCHIVE_CAPACITY_CRITICAL" if status == "critical" else "ARCHIVE_CAPACITY_WARNING"
        )
        summary = (
            f"PostgreSQL 备份盘使用率为 {used_percent:.2f}%，"
            f"当前告警阈值为 {triggered_threshold}%。"
        )
    return WorkflowAlert(
        workflow_run_id=uuid5(
            NAMESPACE_URL,
            f"macro-data-platform:archive-capacity:{report_date.isoformat()}:{status}",
        ),
        report_date=report_date,
        stage="scheduler",
        error_code=error_code,
        summary=summary,
        safe_retry=(
            "先确认 /archive 仍挂载到独立物理盘并检查最近备份；"
            "按保留策略清理或扩容，禁止直接删除唯一可恢复备份。"
        ),
    )

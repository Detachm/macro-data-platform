from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from macro_platform.jobs import archive_monitor
from macro_platform.services.workflow_alerts import WorkflowAlert


class _Delivery:
    def __init__(self, status: str = "succeeded") -> None:
        self.status = status
        self.alerts: list[WorkflowAlert] = []

    async def deliver(self, alert: WorkflowAlert) -> SimpleNamespace:
        self.alerts.append(alert)
        return SimpleNamespace(status=self.status)


@pytest.mark.asyncio
async def test_archive_monitor_does_not_alert_below_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        archive_monitor.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=69),
    )
    delivery = _Delivery()

    exit_code = await archive_monitor.monitor_archive_capacity(
        path=tmp_path,
        warning_percent=70,
        critical_percent=85,
        delivery=delivery,
    )

    assert exit_code == 0
    assert delivery.alerts == []


@pytest.mark.asyncio
async def test_archive_monitor_sends_one_deterministic_daily_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        archive_monitor.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=70),
    )
    delivery = _Delivery()
    now = datetime(2026, 7, 28, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    first = await archive_monitor.monitor_archive_capacity(
        path=tmp_path,
        warning_percent=70,
        critical_percent=85,
        delivery=delivery,
        now=now,
    )
    second = await archive_monitor.monitor_archive_capacity(
        path=tmp_path,
        warning_percent=70,
        critical_percent=85,
        delivery=delivery,
        now=now,
    )

    assert first == second == 0
    assert [alert.error_code for alert in delivery.alerts] == [
        "ARCHIVE_CAPACITY_WARNING",
        "ARCHIVE_CAPACITY_WARNING",
    ]
    assert delivery.alerts[0].workflow_run_id == delivery.alerts[1].workflow_run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("used", [85, 99])
async def test_archive_monitor_alerts_and_fails_at_the_critical_threshold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    used: int,
) -> None:
    monkeypatch.setattr(
        archive_monitor.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=used),
    )
    delivery = _Delivery()

    exit_code = await archive_monitor.monitor_archive_capacity(
        path=tmp_path,
        warning_percent=70,
        critical_percent=85,
        delivery=delivery,
    )

    assert exit_code == 1
    assert delivery.alerts[0].error_code == "ARCHIVE_CAPACITY_CRITICAL"


@pytest.mark.asyncio
async def test_archive_monitor_alerts_when_the_mount_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def unavailable(_path: Path) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(archive_monitor.shutil, "disk_usage", unavailable)
    delivery = _Delivery()

    exit_code = await archive_monitor.monitor_archive_capacity(
        path=tmp_path,
        warning_percent=70,
        critical_percent=85,
        delivery=delivery,
    )

    assert exit_code == 1
    assert delivery.alerts[0].error_code == "ARCHIVE_FILESYSTEM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_archive_monitor_rejects_a_smaller_fallback_filesystem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        archive_monitor.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10, used=1),
    )
    delivery = _Delivery()

    exit_code = await archive_monitor.monitor_archive_capacity(
        path=tmp_path,
        warning_percent=70,
        critical_percent=85,
        minimum_total_bytes=20,
        delivery=delivery,
    )

    assert exit_code == 1
    assert delivery.alerts[0].error_code == "ARCHIVE_FILESYSTEM_UNAVAILABLE"

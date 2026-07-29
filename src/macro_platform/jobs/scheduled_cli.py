"""Command-line boundary for the scheduled-ingestion worker."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
from contextlib import suppress
from datetime import date
from pathlib import Path

from macro_platform.config import Settings, get_settings
from macro_platform.jobs.scheduled_runtime import run_scheduler
from macro_platform.observability import configure_logging
from macro_platform.services.workflow_operations import PostgresWorkerReadinessReader
from macro_platform.storage.database import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the macro-data scheduled ingestion worker")
    parser.add_argument("--report-date", type=_parse_report_date, help="run one ISO report date")
    parser.add_argument(
        "--report-version",
        help="explicit immutable report version for a date-scoped regeneration",
    )
    parser.add_argument(
        "--backfill-start", type=_parse_report_date, help="inclusive ISO start date"
    )
    parser.add_argument("--backfill-end", type=_parse_report_date, help="inclusive ISO end date")
    parser.add_argument(
        "--check-ready",
        action="store_true",
        help="check database, schema, provider, and delivery configuration, then exit",
    )
    arguments = parser.parse_args()
    if arguments.check_ready and any(
        value is not None
        for value in (
            arguments.report_date,
            arguments.report_version,
            arguments.backfill_start,
            arguments.backfill_end,
        )
    ):
        parser.error("--check-ready cannot be combined with report or backfill arguments")
    if arguments.report_date is not None and arguments.backfill_start is not None:
        parser.error("--report-date cannot be combined with --backfill-start/--backfill-end")
    if arguments.report_version is not None and arguments.report_date is None:
        parser.error("--report-version requires --report-date")
    if (arguments.backfill_start is None) != (arguments.backfill_end is None):
        parser.error("--backfill-start and --backfill-end must be provided together")
    settings = get_settings()
    configure_logging(settings.log_level)
    if arguments.check_ready:
        raise SystemExit(asyncio.run(_worker_readiness_exit_code(settings)))
    with suppress(KeyboardInterrupt):
        asyncio.run(
            run_scheduler(
                settings=settings,
                run_once_report_date=arguments.report_date,
                report_version=arguments.report_version,
                backfill_start_date=arguments.backfill_start,
                backfill_end_date=arguments.backfill_end,
            )
        )


async def _worker_readiness_exit_code(settings: Settings) -> int:
    database = Database(settings.database_url)
    try:
        result = await PostgresWorkerReadinessReader(database, settings).check()
        if not _xtquant_runtime_available():
            result = result.model_copy(
                update={
                    "status": "not_ready",
                    "unmet_requirements": (
                        *result.unmet_requirements,
                        "HK_XTQUANT_RUNTIME_MISSING",
                    ),
                }
            )
        print(result.model_dump_json())
        return 0 if result.status == "ready" else 1
    finally:
        await database.dispose()


def _xtquant_runtime_available() -> bool:
    data_path = os.environ.get("HK_XTQUANT_DATA_PATH", "").strip()
    if not data_path:
        return False
    resolved_data_path = Path(data_path)
    if not resolved_data_path.is_dir() or not os.access(resolved_data_path, os.R_OK | os.X_OK):
        return False
    try:
        importlib.import_module("xtquant.xtdata")
    except (ImportError, OSError):
        return False
    return True


def _parse_report_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("report dates must use YYYY-MM-DD") from error

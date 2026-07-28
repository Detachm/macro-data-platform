"""Command-line boundary for the scheduled-ingestion worker."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date

from macro_platform.config import get_settings
from macro_platform.jobs.scheduled_runtime import run_scheduler
from macro_platform.observability import configure_logging


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
    arguments = parser.parse_args()
    if arguments.report_date is not None and arguments.backfill_start is not None:
        parser.error("--report-date cannot be combined with --backfill-start/--backfill-end")
    if arguments.report_version is not None and arguments.report_date is None:
        parser.error("--report-version requires --report-date")
    if (arguments.backfill_start is None) != (arguments.backfill_end is None):
        parser.error("--backfill-start and --backfill-end must be provided together")
    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(
        run_scheduler(
            settings=settings,
            run_once_report_date=arguments.report_date,
            report_version=arguments.report_version,
            backfill_start_date=arguments.backfill_start,
            backfill_end_date=arguments.backfill_end,
        )
    )


def _parse_report_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("report dates must use YYYY-MM-DD") from error

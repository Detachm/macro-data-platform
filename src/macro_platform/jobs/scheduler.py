from __future__ import annotations

import asyncio
import signal

import structlog

from macro_platform.config import get_settings
from macro_platform.observability import configure_logging


async def run_scheduler() -> None:
    """Idle scheduler shell; regional job registration lands in dedicated issues."""

    logger = structlog.get_logger("scheduler")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stop.set)

    await logger.ainfo("scheduler_started", registered_jobs=0)
    await stop.wait()
    await logger.ainfo("scheduler_stopped")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(run_scheduler())


if __name__ == "__main__":
    main()

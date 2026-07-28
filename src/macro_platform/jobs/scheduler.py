"""Compatibility façade for the scheduled-ingestion subsystem.

The public names remain stable for workers and tests.  Runtime composition,
durable task checkpointing, and date-level orchestration live in focused
modules so each has one operational responsibility.
"""

from macro_platform.jobs.scheduled_cli import main
from macro_platform.jobs.scheduled_runtime import (
    _first_safe_run_time,
    _run_configured_schedule,
    build_registered_tasks,
    run_scheduler,
)
from macro_platform.jobs.scheduled_tasks import (
    CheckpointedScheduledTask,
    PostgresScheduledTaskCheckpointStore,
)
from macro_platform.jobs.scheduled_types import (
    ReportInputMaterializer,
    ScheduledRequestFactory,
    ScheduledTask,
    ScheduledTaskCheckpointStore,
    ScheduledTaskExecutor,
    ScheduledTaskResult,
    ScheduledTaskStatus,
    ScheduledWorkerResult,
    ScheduledWorkerStatus,
)
from macro_platform.jobs.scheduled_worker import (
    PostgresReportDateLock,
    ReportDateLock,
    RetryableScheduledTaskError,
    ScheduledIngestionWorker,
    SchedulerNotConfiguredError,
)

__all__ = [
    "CheckpointedScheduledTask",
    "PostgresReportDateLock",
    "PostgresScheduledTaskCheckpointStore",
    "ReportDateLock",
    "ReportInputMaterializer",
    "RetryableScheduledTaskError",
    "ScheduledIngestionWorker",
    "ScheduledRequestFactory",
    "ScheduledTask",
    "ScheduledTaskCheckpointStore",
    "ScheduledTaskExecutor",
    "ScheduledTaskResult",
    "ScheduledTaskStatus",
    "ScheduledWorkerResult",
    "ScheduledWorkerStatus",
    "SchedulerNotConfiguredError",
    "_first_safe_run_time",
    "_run_configured_schedule",
    "build_registered_tasks",
    "main",
    "run_scheduler",
]

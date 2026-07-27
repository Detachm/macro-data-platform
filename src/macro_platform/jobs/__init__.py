from macro_platform.jobs.runner import (
    CheckpointedIngestJobHandler,
    IngestionExecutionContext,
    IngestJobHandler,
    JobRunner,
)

__all__ = [
    "CheckpointedIngestJobHandler",
    "CnBaoStockIngestHandler",
    "HkXtQuantIngestHandler",
    "IngestionExecutionContext",
    "IngestJobHandler",
    "JobRunner",
]


def __getattr__(name: str) -> object:
    """Load optional live-provider handlers only when callers ask for them."""

    if name == "CnBaoStockIngestHandler":
        from macro_platform.jobs.cn_baostock_ingestion import CnBaoStockIngestHandler

        return CnBaoStockIngestHandler
    if name == "HkXtQuantIngestHandler":
        from macro_platform.jobs.hk_xtquant_ingestion import HkXtQuantIngestHandler

        return HkXtQuantIngestHandler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

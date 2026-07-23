from __future__ import annotations

from typing import Protocol

from macro_platform.contracts.provider import IngestJobRequest, IngestJobResult


class IngestJobHandler(Protocol):
    async def run(self, request: IngestJobRequest) -> IngestJobResult: ...


class JobRunner:
    """Execution seam for retries, locks, checkpoints, and metrics."""

    def __init__(self, handler: IngestJobHandler) -> None:
        self._handler = handler

    async def execute(self, request: IngestJobRequest) -> IngestJobResult:
        result = await self._handler.run(request)
        if result.provider_role != request.provider_role or result.dataset != request.dataset:
            raise ValueError("job result does not match its request")
        return result

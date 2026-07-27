from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import Field

from macro_platform.contracts.common import StrictModel


class LlmError(RuntimeError):
    """Base error for the report-generation model boundary."""


class LlmTimeoutError(LlmError):
    pass


class LlmStructuredOutputError(LlmError):
    pass


class LlmRequest(StrictModel):
    model: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=64)
    system_prompt: str = Field(min_length=1, max_length=10000)
    input_payload: dict[str, Any]
    input_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_ref_ids: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class LlmResponse(StrictModel):
    structured_output: dict[str, Any]
    model: str | None = None
    usage: Mapping[str, int] = Field(default_factory=dict)


class LlmClient(Protocol):
    async def generate(self, request: LlmRequest) -> LlmResponse: ...

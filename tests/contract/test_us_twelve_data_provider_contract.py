from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from macro_platform.contracts.common import Region
from macro_platform.contracts.market import Adjustment, BarQuery, Interval
from macro_platform.contracts.provider import Dataset, FetchContext
from macro_platform.providers.us.twelve_data import (
    TWELVE_DATA_PROVIDER_ID,
    TwelveDataDailyBarsProvider,
    TwelveDataInstrument,
)
from tests.contract.provider_suite import (
    assert_available_at_not_after_as_of,
    assert_capabilities_contract,
    assert_page_contract,
    assert_page_provenance,
)

NOW = datetime(2026, 7, 23, 21, 0, tzinfo=UTC)
CONTEXT = FetchContext(
    request_id=UUID("00000000-0000-4000-8000-000000000034"),
    as_of=NOW,
    deadline_at=datetime(2026, 7, 23, 21, 1, tzinfo=UTC),
)
SPY = TwelveDataInstrument(
    instrument_id="ins_us_etf_spy",
    canonical_symbol="ARCX:SPY",
    source_symbol="SPY",
)
FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "us" / "twelve_data"


def _provider(payload: dict[str, object]) -> TwelveDataDailyBarsProvider:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload, request=request)
        )
    )
    return TwelveDataDailyBarsProvider(
        api_key=SecretStr("contract-test-api-key"),
        instruments=[SPY],
        client=client,
        cursor_signing_secret="contract-test-cursor-secret",
        clock=lambda: NOW,
    )


def _fixture_payload(name: str) -> dict[str, object]:
    payload = json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _query() -> BarQuery:
    return BarQuery(
        instrument_ids=[SPY.instrument_id],
        interval=Interval.D1,
        start=datetime(2026, 7, 21, 4, 0, tzinfo=UTC),
        end=datetime(2026, 7, 23, 4, 0, tzinfo=UTC),
        adjustment=Adjustment.RAW,
        as_of=NOW,
        limit=2,
    )


@pytest.mark.asyncio
async def test_PRV_001_PRV_012_PRV_017_live_daily_bars_meet_shared_provider_contract() -> None:
    provider = _provider(_fixture_payload("success.json"))
    try:
        capabilities = assert_capabilities_contract(provider)
        page = await provider.fetch_bars(_query(), CONTEXT)
    finally:
        await provider.aclose()

    assert capabilities.provider_id == TWELVE_DATA_PROVIDER_ID
    assert capabilities.regions == {Region.US}
    assert capabilities.datasets == {Dataset.BARS}
    assert capabilities.intervals == {Interval.D1}
    assert capabilities.external_llm_allowed is False
    assert_page_contract(page)
    assert_page_provenance(page, TWELVE_DATA_PROVIDER_ID)
    assert_available_at_not_after_as_of(page, NOW)
    assert page.complete is True
    assert [item.trading_date.isoformat() for item in page.items] == ["2026-07-21", "2026-07-22"]


@pytest.mark.asyncio
async def test_PRV_009_quarantines_a_malformed_ohlcv_record_without_empty_success() -> None:
    provider = _provider(_fixture_payload("missing_fields.json"))
    try:
        page = await provider.fetch_bars(_query(), CONTEXT)
    finally:
        await provider.aclose()

    assert page.items == []
    assert [warning.code for warning in page.warnings] == ["PROVIDER_RECORD_QUARANTINED"]
    rejection = page.warnings[0].details["rejection"]
    assert rejection["error_code"] == "PROVIDER_SCHEMA_CHANGED"
    assert rejection["redacted_payload"] == {
        "datetime": "2026-07-22",
        "fields": ["datetime", "high", "low", "open", "volume"],
    }


def test_PRV_009_fixture_manifest_is_complete_and_credential_free() -> None:
    manifest = _fixture_payload("manifest.json")

    assert manifest["provider_id"] == TWELVE_DATA_PROVIDER_ID
    names = manifest["fixtures"]
    assert isinstance(names, list)
    for name in names:
        assert isinstance(name, str)
        fixture_text = (FIXTURE_ROOT / name).read_text(encoding="utf-8").lower()
        assert "apikey" not in fixture_text
        assert "api key" not in fixture_text

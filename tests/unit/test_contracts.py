from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from macro_platform.contracts.common import AvailabilityBasis, Region
from macro_platform.contracts.market import Adjustment, Interval, MarketBar
from macro_platform.contracts.news import ContentMode
from tests.helpers import NOW, news_event, source_ref


def valid_bar() -> MarketBar:
    return MarketBar(
        bar_id="bar_fixture_1",
        instrument_id="ins_fixture_1",
        canonical_symbol="XSHG:600000",
        region=Region.CN,
        interval=Interval.D1,
        bar_start=datetime(2026, 7, 22, tzinfo=UTC),
        bar_end=datetime(2026, 7, 23, tzinfo=UTC),
        trading_date=datetime(2026, 7, 22, tzinfo=UTC).date(),
        open=Decimal("10.10"),
        high=Decimal("10.80"),
        low=Decimal("9.90"),
        close=Decimal("10.60"),
        volume=Decimal("1000000"),
        currency="CNY",
        adjustment=Adjustment.RAW,
        available_at=NOW,
        availability_basis=AvailabilityBasis.EXCHANGE_PUBLISHED,
        source=source_ref(),
    )


def test_unit_007_market_bar_serializes_decimal_without_float_loss() -> None:
    payload = valid_bar().model_dump(mode="json")
    assert payload["close"] == "10.60"
    assert Decimal(payload["close"]) == Decimal("10.60")


def test_market_bar_rejects_impossible_ohlc() -> None:
    with pytest.raises(ValidationError, match="within low/high"):
        valid_bar().model_copy(update={"high": Decimal("10.20")}).model_validate(
            valid_bar().model_dump() | {"high": Decimal("10.20")}
        )


def test_adjusted_bar_requires_adjustment_timestamp() -> None:
    payload = valid_bar().model_dump()
    payload["adjustment"] = Adjustment.TOTAL_RETURN
    with pytest.raises(ValidationError, match="adjustment_as_of"):
        MarketBar.model_validate(payload)


def test_prv_020_public_contract_rejects_unknown_fields() -> None:
    payload = valid_bar().model_dump()
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        MarketBar.model_validate(payload)


def test_time_002_contract_requires_timezone_aware_datetime() -> None:
    payload = valid_bar().model_dump()
    payload["available_at"] = datetime(2026, 7, 23, 8, 0)
    with pytest.raises(ValidationError, match="timezone"):
        MarketBar.model_validate(payload)


def test_news_body_requires_full_text_mode() -> None:
    with pytest.raises(ValidationError, match="full_text"):
        news_event(content_mode=ContentMode.SNIPPET, body="full article")


def test_full_text_cannot_be_retained_without_storage_rights() -> None:
    payload = news_event(content_mode=ContentMode.FULL_TEXT, body="full article").model_dump()
    payload["usage_rights"]["storage_allowed"] = False
    with pytest.raises(ValidationError, match="cannot be retained"):
        type(news_event()).model_validate(payload)

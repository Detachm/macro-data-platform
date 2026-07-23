from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from macro_platform.contracts.common import Region
from macro_platform.normalization.cn_hk import (
    CN_TIMEZONE,
    HK_TIMEZONE,
    SymbolMappingError,
    TimeMappingError,
    TradingCalendarMappingError,
    UnitMappingError,
    normalize_instrument_symbol,
    normalize_timestamp,
    normalize_trading_date,
    normalize_unit,
)
from macro_platform.normalization.common import canonical_json_checksum

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "normalization" / "cn_hk"
REQUIRED_SCENARIOS = {
    "success",
    "empty",
    "missing_fields",
    "auth_failure",
    "rate_limited",
    "timeout",
    "schema_changed",
    "duplicate_page",
}


def _load_fixture(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((FIXTURE_DIR / f"{name}.json").read_text()))


def test_fixtures_cover_required_scenarios() -> None:
    for fixture_name in ("cn", "hk"):
        fixture = _load_fixture(fixture_name)
        scenarios = cast(dict[str, Any], fixture["scenarios"])
        assert set(scenarios) == REQUIRED_SCENARIOS


@pytest.mark.parametrize(
    ("fixture_name", "case_id", "expected_symbol", "expected_timezone"),
    [
        ("cn", "SYM-001", "600519", CN_TIMEZONE),
        ("cn", "SYM-002", "000001", CN_TIMEZONE),
        ("hk", "SYM-007", "00700", HK_TIMEZONE),
        ("hk", "SYM-008", "00700", HK_TIMEZONE),
    ],
)
def test_symbol_success_cases_from_fixtures(
    fixture_name: str,
    case_id: str,
    expected_symbol: str,
    expected_timezone: str,
) -> None:
    fixture = _load_fixture(fixture_name)
    region = Region(fixture["region"])
    success = cast(dict[str, Any], cast(dict[str, Any], fixture["scenarios"])["success"])
    row = next(
        row for row in cast(list[dict[str, str]], success["symbols"]) if row["case_id"] == case_id
    )

    first = normalize_instrument_symbol(
        region=region,
        venue_mic=row["venue_mic"],
        local_symbol=row["local_symbol"],
        valid_from=date.fromisoformat(row["valid_from"]),
        provider_id=row["provider_id"],
    )
    second = normalize_instrument_symbol(
        region=region,
        venue_mic=row["venue_mic"],
        local_symbol=row["local_symbol"],
        valid_from=date.fromisoformat(row["valid_from"]),
        provider_id=row["provider_id"],
    )

    assert first == second
    assert first.local_symbol == expected_symbol
    assert first.timezone == expected_timezone
    assert first.canonical_symbol == f"{row['venue_mic']}:{expected_symbol}"
    assert first.instrument_id.startswith("ins_")
    assert len(first.instrument_id) == 30
    assert canonical_json_checksum(first.__dict__) == canonical_json_checksum(second.__dict__)


def test_sym_003_cn_does_not_guess_exchange_from_numeric_code() -> None:
    with pytest.raises(SymbolMappingError, match="venue"):
        normalize_instrument_symbol(
            region=Region.CN,
            venue_mic="",
            local_symbol="600519",
            valid_from=date(2001, 8, 27),
            provider_id="sse_public_list",
        )


def test_sym_004_cn_rejects_wrong_venue() -> None:
    with pytest.raises(SymbolMappingError, match="CN venue"):
        normalize_instrument_symbol(
            region=Region.CN,
            venue_mic="XHKG",
            local_symbol="600519",
            valid_from=date(2001, 8, 27),
            provider_id="sse_public_list",
        )


def test_sym_005_alias_effective_dates_are_preserved() -> None:
    actual = normalize_instrument_symbol(
        region=Region.CN,
        venue_mic="XSHG",
        local_symbol="600519",
        valid_from=date(2001, 8, 27),
        valid_to=date(2026, 7, 23),
        provider_id="sse_public_list",
    )

    assert actual.aliases[0].valid_from == date(2001, 8, 27)
    assert actual.aliases[0].valid_to == date(2026, 7, 23)
    assert actual.aliases[0].source_symbol == "600519"


def test_sym_006_stable_instrument_id_repeats_for_same_seed() -> None:
    kwargs = {
        "region": Region.CN,
        "venue_mic": "XSHG",
        "local_symbol": "600519",
        "valid_from": date(2001, 8, 27),
        "provider_id": "sse_public_list",
    }

    assert (
        normalize_instrument_symbol(**kwargs).instrument_id
        == normalize_instrument_symbol(**kwargs).instrument_id
    )


def test_sym_009_hk_rejects_symbols_that_cannot_be_five_digit_codes() -> None:
    with pytest.raises(SymbolMappingError, match="one to five digits"):
        normalize_instrument_symbol(
            region=Region.HK,
            venue_mic="XHKG",
            local_symbol="100000",
            valid_from=date(2026, 1, 1),
            provider_id="hkex_securities_lists",
        )


def test_sym_010_hk_rejects_wrong_venue() -> None:
    with pytest.raises(SymbolMappingError, match="HK venue"):
        normalize_instrument_symbol(
            region=Region.HK,
            venue_mic="XSHE",
            local_symbol="00700",
            valid_from=date(2004, 6, 16),
            provider_id="hkex_securities_lists",
        )


def test_sym_011_alias_effective_date_range_is_validated() -> None:
    with pytest.raises(SymbolMappingError, match="valid_to"):
        normalize_instrument_symbol(
            region=Region.HK,
            venue_mic="XHKG",
            local_symbol="00700",
            valid_from=date(2004, 6, 16),
            valid_to=date(2004, 6, 15),
            provider_id="hkex_securities_lists",
        )


def test_sym_012_repeated_symbol_normalization_is_idempotent() -> None:
    first = normalize_instrument_symbol(
        region=Region.HK,
        venue_mic="XHKG",
        local_symbol="700",
        valid_from=date(2004, 6, 16),
        provider_id="hkex_securities_lists",
    )
    second = normalize_instrument_symbol(
        region=first.region,
        venue_mic=first.venue_mic,
        local_symbol=first.local_symbol,
        valid_from=first.aliases[0].valid_from,
        provider_id=first.aliases[0].provider_id,
    )

    assert first.local_symbol == "00700"
    assert first.aliases[0].source_symbol == "700"
    assert second.local_symbol == first.local_symbol
    assert second.instrument_id == first.instrument_id
    assert second.canonical_symbol == first.canonical_symbol


def test_time_001_cn_hk_timestamps_convert_to_utc_from_fixture() -> None:
    for fixture_name in ("cn", "hk"):
        fixture = _load_fixture(fixture_name)
        region = Region(fixture["region"])
        success = cast(dict[str, Any], cast(dict[str, Any], fixture["scenarios"])["success"])
        row = cast(list[dict[str, str]], success["timestamps"])[0]

        actual = normalize_timestamp(region=region, value=datetime.fromisoformat(row["value"]))

        assert actual.utc.tzinfo is UTC


def test_time_002_naive_timestamp_is_rejected() -> None:
    with pytest.raises(TimeMappingError, match="timezone"):
        normalize_timestamp(region=Region.CN, value=datetime(2026, 7, 23, 15, 0))


def test_time_004_local_trading_date_is_preserved() -> None:
    actual = normalize_timestamp(
        region=Region.CN,
        value=datetime.fromisoformat("2026-07-23T23:30:00+08:00"),
        trading_date=date(2026, 7, 23),
    )

    assert actual.utc == datetime(2026, 7, 23, 15, 30, tzinfo=UTC)
    assert actual.local_trading_date == date(2026, 7, 23)


def test_time_010_trading_calendar_rejects_weekends_and_fixture_holidays() -> None:
    assert normalize_trading_date(region=Region.HK, value=date(2026, 7, 23)) == date(2026, 7, 23)

    with pytest.raises(TradingCalendarMappingError, match="not a trading day"):
        normalize_trading_date(region=Region.HK, value=date(2026, 7, 25))

    with pytest.raises(TradingCalendarMappingError, match="not a trading day"):
        normalize_trading_date(
            region=Region.CN,
            value=date(2026, 10, 1),
            holidays=frozenset({date(2026, 10, 1)}),
        )


def test_unit_001_decimal_strings_are_normalized_without_float() -> None:
    actual = normalize_unit(region=Region.CN, value="12.3400", source_unit="亿元")

    assert actual.value == Decimal("1234000000.0000")
    assert actual.currency == "CNY"
    assert isinstance(actual.value, Decimal)


def test_unit_002_float_inputs_are_rejected() -> None:
    with pytest.raises(UnitMappingError, match="Decimal"):
        normalize_unit(region=Region.CN, value=cast(Any, 1.2), source_unit="元")


def test_unit_003_percent_units_keep_percent_semantics() -> None:
    actual = normalize_unit(region=Region.CN, value="5.2", source_unit="%")

    assert actual.value == Decimal("5.2")
    assert actual.unit == "percent"
    assert actual.currency is None
    assert actual.is_percent


def test_unit_009_hk_hkd_units_preserve_currency_and_scale() -> None:
    actual = normalize_unit(region=Region.HK, value="3.5", source_unit="HKD mn")

    assert actual.value == Decimal("3500000.0")
    assert actual.unit == "HKD"
    assert actual.currency == "HKD"


def test_unit_normalization_is_idempotent_for_same_input() -> None:
    assert normalize_unit(region=Region.HK, value="3.5", source_unit="HKD mn") == normalize_unit(
        region=Region.HK,
        value="3.5",
        source_unit="HKD mn",
    )

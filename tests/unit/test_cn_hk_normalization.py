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
    InstrumentAliasRegistryEntry,
    MissingUnitValue,
    NormalizedUnit,
    SymbolMappingError,
    TimeMappingError,
    TradingCalendarMappingError,
    UnitMappingError,
    normalize_instrument_symbol,
    normalize_optional_unit,
    normalize_timestamp,
    normalize_trading_date,
    normalize_unit,
    resolve_instrument_alias,
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


def _alias(
    instrument_key: str,
    source_symbol: str,
    venue_mic: str,
    valid_from: date,
    valid_to: date | None = None,
) -> InstrumentAliasRegistryEntry:
    return InstrumentAliasRegistryEntry(
        instrument_key=instrument_key,
        provider_id="registry.synthetic",
        source_symbol=source_symbol,
        venue_mic=venue_mic,
        valid_from=valid_from,
        valid_to=valid_to,
    )


def test_fixtures_cover_required_scenarios() -> None:
    for fixture_name in ("cn", "hk"):
        fixture = _load_fixture(fixture_name)
        scenarios = cast(dict[str, Any], fixture["scenarios"])
        assert set(scenarios) == REQUIRED_SCENARIOS


def test_sym_001_sh600519_exchange_sse_maps_to_xshg_and_preserves_source() -> None:
    actual = normalize_instrument_symbol(
        region=Region.CN,
        venue_mic="XSHG",
        local_symbol="sh600519",
        valid_from=date(2001, 8, 27),
        provider_id="sse_public_list",
        instrument_key="cn-security-kweichow-moutai",
    )

    assert actual.canonical_symbol == "XSHG:600519"
    assert actual.local_symbol == "600519"
    assert actual.aliases[0].source_symbol == "sh600519"
    assert actual.timezone == CN_TIMEZONE


def test_sym_002_sz_suffix_maps_to_xshe() -> None:
    actual = normalize_instrument_symbol(
        region=Region.CN,
        venue_mic="XSHE",
        local_symbol="000001.SZ",
        valid_from=date(1991, 4, 3),
        provider_id="szse_public_list",
        instrument_key="cn-security-pingan-bank",
    )

    assert actual.canonical_symbol == "XSHE:000001"


def test_sym_003_hk_suffix_preserves_five_digit_padding() -> None:
    actual = normalize_instrument_symbol(
        region=Region.HK,
        venue_mic="XHKG",
        local_symbol="700.HK",
        valid_from=date(2004, 6, 16),
        provider_id="hkex_securities_lists",
        instrument_key="hk-security-tencent",
    )

    assert actual.canonical_symbol == "XHKG:00700"
    assert actual.local_symbol == "00700"
    assert actual.aliases[0].source_symbol == "700.HK"
    assert actual.timezone == HK_TIMEZONE


def test_sym_006_same_numeric_code_on_two_exchanges_keeps_distinct_mics() -> None:
    sh = normalize_instrument_symbol(
        region=Region.CN,
        venue_mic="XSHG",
        local_symbol="000001",
        valid_from=date(2026, 1, 1),
        provider_id="synthetic_registry",
        instrument_key="cn-security-sh-000001",
    )
    sz = normalize_instrument_symbol(
        region=Region.CN,
        venue_mic="XSHE",
        local_symbol="000001",
        valid_from=date(1991, 4, 3),
        provider_id="synthetic_registry",
        instrument_key="cn-security-pingan-bank",
    )

    assert sh.canonical_symbol == "XSHG:000001"
    assert sz.canonical_symbol == "XSHE:000001"
    assert sh.instrument_id != sz.instrument_id


def test_sym_007_unknown_symbol_without_exchange_is_unresolved() -> None:
    with pytest.raises(SymbolMappingError, match="venue"):
        normalize_instrument_symbol(
            region=Region.CN,
            venue_mic="",
            local_symbol="UNKNOWN",
            valid_from=date(2026, 1, 1),
            provider_id="synthetic_registry",
            instrument_key="unknown",
        )


def test_sym_008_old_and_new_aliases_keep_one_instrument_id() -> None:
    aliases = (
        _alias("cn-security-same-company", "600001", "XSHG", date(2000, 1, 1), date(2020, 1, 1)),
        _alias("cn-security-same-company", "688001", "XSHG", date(2020, 1, 2)),
    )

    old = resolve_instrument_alias(
        region=Region.CN,
        venue_mic="XSHG",
        local_symbol="600001",
        as_of=date(2019, 12, 31),
        provider_id="sse_public_list",
        aliases=aliases,
    )
    new = resolve_instrument_alias(
        region=Region.CN,
        venue_mic="XSHG",
        local_symbol="688001",
        as_of=date(2026, 1, 1),
        provider_id="sse_public_list",
        aliases=aliases,
    )

    assert old.instrument_key == new.instrument_key
    assert old.instrument_id == new.instrument_id
    assert old.canonical_symbol == "XSHG:600001"
    assert new.canonical_symbol == "XSHG:688001"


def test_sym_009_same_day_alias_to_two_instruments_is_ambiguous() -> None:
    aliases = (
        _alias("hk-security-a", "700", "XHKG", date(2020, 1, 1)),
        _alias("hk-security-b", "00700", "XHKG", date(2020, 1, 1)),
    )

    with pytest.raises(SymbolMappingError, match="ambiguous"):
        resolve_instrument_alias(
            region=Region.HK,
            venue_mic="XHKG",
            local_symbol="700.HK",
            as_of=date(2026, 1, 1),
            provider_id="hkex_securities_lists",
            aliases=aliases,
        )


def test_sym_010_already_canonical_hk_symbol_is_idempotent() -> None:
    first = normalize_instrument_symbol(
        region=Region.HK,
        venue_mic="XHKG",
        local_symbol="XHKG:00700",
        valid_from=date(2004, 6, 16),
        provider_id="hkex_securities_lists",
        instrument_key="hk-security-tencent",
    )
    second = normalize_instrument_symbol(
        region=first.region,
        venue_mic=first.venue_mic,
        local_symbol=first.canonical_symbol,
        valid_from=first.aliases[0].valid_from,
        provider_id=first.aliases[0].provider_id,
        instrument_key=first.instrument_key,
    )

    assert first == second


def test_sym_011_empty_source_symbol_is_quarantined() -> None:
    with pytest.raises(SymbolMappingError, match="local_symbol"):
        normalize_instrument_symbol(
            region=Region.HK,
            venue_mic="XHKG",
            local_symbol="",
            valid_from=date(2026, 1, 1),
            provider_id="hkex_securities_lists",
            instrument_key="empty-symbol",
        )


def test_sym_012_inactive_alias_requires_include_inactive() -> None:
    aliases = (_alias("hk-security-oldco", "00123", "XHKG", date(2000, 1, 1), date(2020, 1, 1)),)

    with pytest.raises(SymbolMappingError, match="inactive"):
        resolve_instrument_alias(
            region=Region.HK,
            venue_mic="XHKG",
            local_symbol="123.HK",
            as_of=date(2026, 1, 1),
            provider_id="hkex_securities_lists",
            aliases=aliases,
        )

    inactive = resolve_instrument_alias(
        region=Region.HK,
        venue_mic="XHKG",
        local_symbol="123.HK",
        as_of=date(2026, 1, 1),
        provider_id="hkex_securities_lists",
        aliases=aliases,
        include_inactive=True,
    )
    assert inactive.canonical_symbol == "XHKG:00123"


def test_symbol_normalization_is_deterministic_for_same_registry_identity() -> None:
    first = normalize_instrument_symbol(
        region=Region.CN,
        venue_mic="XSHG",
        local_symbol="600519",
        valid_from=date(2001, 8, 27),
        provider_id="sse_public_list",
        instrument_key="cn-security-kweichow-moutai",
    )
    second = normalize_instrument_symbol(
        region=Region.CN,
        venue_mic="XSHG",
        local_symbol="600519",
        valid_from=date(2001, 8, 27),
        provider_id="sse_public_list",
        instrument_key="cn-security-kweichow-moutai",
    )

    assert first.instrument_id == second.instrument_id
    assert canonical_json_checksum(first.__dict__) == canonical_json_checksum(second.__dict__)


def test_time_001_cn_local_timestamp_converts_to_utc() -> None:
    actual = normalize_timestamp(
        region=Region.CN,
        value=datetime.fromisoformat("2026-07-23T09:30:00+08:00"),
    )

    assert actual.utc == datetime(2026, 7, 23, 1, 30, tzinfo=UTC)


def test_time_002_naive_timestamp_is_rejected() -> None:
    with pytest.raises(TimeMappingError, match="timezone"):
        normalize_timestamp(region=Region.CN, value=datetime(2026, 7, 23, 9, 30))


def test_time_004_hk_local_0930_uses_hong_kong_trading_date() -> None:
    actual = normalize_timestamp(
        region=Region.HK,
        value=datetime.fromisoformat("2026-07-23T09:30:00+08:00"),
    )

    assert actual.utc == datetime(2026, 7, 23, 1, 30, tzinfo=UTC)
    assert actual.local_trading_date == date(2026, 7, 23)
    assert actual.timezone == HK_TIMEZONE


def test_time_010_asia_market_utc_previous_day_keeps_local_trading_date() -> None:
    actual = normalize_timestamp(
        region=Region.HK,
        value=datetime(2026, 7, 22, 16, 30, tzinfo=UTC),
    )

    assert actual.local.isoformat() == "2026-07-23T00:30:00+08:00"
    assert actual.local_trading_date == date(2026, 7, 23)


def test_trading_calendar_rejects_weekends_and_fixture_holidays() -> None:
    assert normalize_trading_date(region=Region.HK, value=date(2026, 7, 23)) == date(2026, 7, 23)

    with pytest.raises(TradingCalendarMappingError, match="not a trading day"):
        normalize_trading_date(region=Region.HK, value=date(2026, 7, 25))

    with pytest.raises(TradingCalendarMappingError, match="not a trading day"):
        normalize_trading_date(
            region=Region.CN,
            value=date(2026, 10, 1),
            holidays=frozenset({date(2026, 10, 1)}),
        )


def test_unit_001_percent_value_keeps_percent_semantics() -> None:
    actual = normalize_unit(region=Region.CN, value="5.2", source_unit="%")

    assert actual.value == Decimal("5.2")
    assert actual.unit == "percent"
    assert actual.currency is None
    assert actual.is_percent


def test_unit_002_basis_points_convert_to_percent() -> None:
    actual = normalize_unit(region=Region.CN, value="25", source_unit="bp")

    assert actual.value == Decimal("0.25")
    assert actual.unit == "percent"
    assert actual.is_percent


def test_unit_003_cn_yi_yuan_scales_to_cny() -> None:
    actual = normalize_unit(region=Region.CN, value="1.2", source_unit="亿元")

    assert actual.value == Decimal("120000000.0")
    assert actual.unit == "CNY"
    assert actual.currency == "CNY"


def test_unit_009_vendor_missing_sentinel_stays_null_with_reason() -> None:
    actual = normalize_optional_unit(region=Region.HK, value="N/A", source_unit="HKD mn")

    assert isinstance(actual, MissingUnitValue)
    assert actual.value is None
    assert actual.unit == "HKD"
    assert actual.currency == "HKD"
    assert actual.missing_reason == "vendor_missing"


def test_float_inputs_are_rejected() -> None:
    with pytest.raises(UnitMappingError, match="Decimal"):
        normalize_unit(region=Region.CN, value=cast(Any, 1.2), source_unit="元")


def test_unit_normalization_is_idempotent_for_same_input() -> None:
    first = normalize_unit(region=Region.HK, value="3.5", source_unit="HKD mn")
    second = normalize_unit(region=Region.HK, value="3.5", source_unit="HKD mn")

    assert isinstance(first, NormalizedUnit)
    assert first == second

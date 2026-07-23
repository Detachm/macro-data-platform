from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from macro_platform.normalization.common import TimezoneRequiredError
from macro_platform.normalization.us import (
    US_EQUITY_CALENDAR_VERSION,
    AmbiguousSymbolAliasError,
    NonexistentLocalTimeError,
    SymbolNormalizationError,
    UnitNormalizationError,
    UnsupportedExchangeError,
    UsCalendarUnavailableError,
    UsMarketClosedError,
    normalize_us_alias,
    normalize_us_symbol,
    normalize_us_value,
    resolve_us_alias_for_date,
    to_us_market_utc,
    us_equity_calendar_day,
    us_equity_session_window,
    us_instrument_id,
    us_trading_date,
    validate_us_aliases,
)
from macro_platform.normalization.us.symbols import _normalize_canonical_symbol_parts

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "us" / "normalization"
JsonFixtureCase = dict[str, object]


def load_fixture(name: str) -> list[JsonFixtureCase]:
    payload = json.loads((FIXTURE_DIR / name).read_text())
    if not isinstance(payload, list):
        raise AssertionError(f"fixture {name} must contain a JSON array")
    return cast(list[JsonFixtureCase], payload)


def fixture_case_id(case: JsonFixtureCase) -> str:
    return str(case["id"])


@pytest.mark.parametrize("case", load_fixture("symbol_cases.json"), ids=fixture_case_id)
def test_sym_004_005_us_symbols_normalize_to_explicit_mic(case: JsonFixtureCase) -> None:
    actual = normalize_us_symbol(
        source_symbol=str(case["source_symbol"]),
        exchange=case["exchange"] if isinstance(case["exchange"], str) else None,
    )

    assert actual.source_symbol == case["source_symbol"]
    assert actual.venue_mic == case["expected_venue_mic"]
    assert actual.canonical_symbol == case["expected_canonical_symbol"]
    assert actual.local_symbol == actual.canonical_symbol.split(":", maxsplit=1)[1]


@pytest.mark.parametrize("_case_id", [pytest.param("SYM-006", id="SYM-006")])
def test_sym_006_same_symbol_on_two_exchanges_does_not_guess_market(_case_id: str) -> None:
    nasdaq = normalize_us_symbol("TEST", exchange="NASDAQ")
    nyse = normalize_us_symbol("TEST", exchange="NYSE")

    assert nasdaq.canonical_symbol == "XNAS:TEST"
    assert nyse.canonical_symbol == "XNYS:TEST"

    with pytest.raises(SymbolNormalizationError, match="SYMBOL_UNRESOLVED"):
        normalize_us_symbol("TEST", exchange=None)


@pytest.mark.parametrize("_case_id", [pytest.param("SYM-007", id="SYM-007")])
def test_sym_007_unknown_symbol_without_exchange_is_unresolved(_case_id: str) -> None:
    with pytest.raises(SymbolNormalizationError, match="SYMBOL_UNRESOLVED"):
        normalize_us_symbol("UNKNOWN", exchange=None)


@pytest.mark.parametrize("_case_id", [pytest.param("SYM-011", id="SYM-011")])
def test_sym_011_us_symbol_rejects_empty_values_and_unknown_mics(_case_id: str) -> None:
    with pytest.raises(SymbolNormalizationError, match="SYMBOL_UNRESOLVED"):
        normalize_us_symbol("   ", exchange="NASDAQ")

    with pytest.raises(UnsupportedExchangeError, match="unsupported MIC"):
        normalize_us_symbol("XXXX:AAPL")

    with pytest.raises(SymbolNormalizationError, match="SYMBOL_UNRESOLVED"):
        normalize_us_symbol("XNAS:   ")


def test_canonical_symbol_conflicting_exchange_is_ambiguous() -> None:
    with pytest.raises(AmbiguousSymbolAliasError, match="AMBIGUOUS_SYMBOL_ALIAS"):
        normalize_us_symbol("XNAS:AAPL", exchange="NYSE")


def test_us_symbol_normalize_is_deterministic() -> None:
    raw = normalize_us_symbol("aapl", exchange="NASDAQ")
    canonical = normalize_us_symbol(raw.canonical_symbol)

    assert raw.source_symbol == "aapl"
    assert canonical.source_symbol == "XNAS:AAPL"
    assert raw == canonical


@pytest.mark.parametrize("_case_id", [pytest.param("SYM-010", id="SYM-010")])
def test_sym_010_canonical_symbol_parts_normalization_is_idempotent(_case_id: str) -> None:
    normalized = _normalize_canonical_symbol_parts("XNAS:aapl")

    assert normalized.canonical_symbol == "XNAS:AAPL"
    assert _normalize_canonical_symbol_parts(normalized.canonical_symbol) == normalized


@pytest.mark.parametrize("_case_id", [pytest.param("SYM-008", id="SYM-008")])
def test_sym_008_old_and_new_alias_can_share_one_stable_instrument_id(_case_id: str) -> None:
    first_valid_from = date(2012, 5, 18)
    first_canonical_symbol = "XNAS:FB"
    old_alias = normalize_us_alias(
        source_symbol="FB",
        exchange="NASDAQ",
        issuer_key="cik0001326801",
        valid_from=first_valid_from,
        valid_to=date(2022, 6, 9),
        instrument_canonical_symbol=first_canonical_symbol,
        instrument_valid_from=first_valid_from,
    )
    new_alias = normalize_us_alias(
        source_symbol="META",
        exchange="NASDAQ",
        issuer_key="cik0001326801",
        valid_from=date(2022, 6, 9),
        instrument_canonical_symbol=first_canonical_symbol,
        instrument_valid_from=first_valid_from,
    )

    assert old_alias.instrument_id == new_alias.instrument_id
    assert old_alias.instrument_id == us_instrument_id(
        first_canonical_symbol,
        valid_from=first_valid_from,
    )
    assert old_alias.canonical_symbol == "XNAS:FB"
    assert new_alias.canonical_symbol == "XNAS:META"
    assert old_alias.issuer_key == new_alias.issuer_key == "cik0001326801"
    validate_us_aliases([old_alias, new_alias])
    assert (
        resolve_us_alias_for_date(
            source_symbol="FB",
            exchange="NASDAQ",
            aliases=[old_alias, new_alias],
            as_of=date(2022, 6, 8),
        )
        == old_alias
    )
    assert (
        resolve_us_alias_for_date(
            source_symbol="META",
            exchange="NASDAQ",
            aliases=[old_alias, new_alias],
            as_of=date(2022, 6, 9),
        )
        == new_alias
    )


def test_us_alias_defaults_instrument_id_to_current_canonical_symbol_and_valid_from() -> None:
    alias = normalize_us_alias(
        source_symbol="AAPL",
        exchange="NASDAQ",
        issuer_key="cik0000320193",
        valid_from=date(2026, 1, 1),
    )

    expected_digest = hashlib.sha256(b"XNAS:AAPL2026-01-01").hexdigest()[:16]
    assert alias.instrument_id == us_instrument_id("XNAS:AAPL", valid_from=date(2026, 1, 1))
    assert alias.instrument_id == f"ins_us_{expected_digest}"


def test_us_alias_rejects_inverted_effective_dates() -> None:
    with pytest.raises(SymbolNormalizationError):
        normalize_us_alias(
            source_symbol="AAPL",
            exchange="NASDAQ",
            issuer_key="cik0000320193",
            valid_from=date(2026, 1, 2),
            valid_to=date(2026, 1, 1),
        )


def test_same_instrument_alias_overlap_is_not_ambiguous() -> None:
    first = normalize_us_alias(
        source_symbol="AAPL",
        exchange="NASDAQ",
        issuer_key="cik0000320193",
        valid_from=date(2026, 1, 1),
    )
    second = normalize_us_alias(
        source_symbol="AAPL",
        exchange="NASDAQ",
        issuer_key="cik0000320193",
        valid_from=date(2026, 1, 1),
    )

    validate_us_aliases([first, second])


@pytest.mark.parametrize("_case_id", [pytest.param("SYM-009", id="SYM-009")])
def test_sym_009_same_alias_date_cannot_point_to_two_instruments(_case_id: str) -> None:
    first = normalize_us_alias(
        source_symbol="TEST",
        exchange="NASDAQ",
        issuer_key="issuer-a",
        valid_from=date(2026, 1, 1),
    )
    second = normalize_us_alias(
        source_symbol="TEST",
        exchange="NASDAQ",
        issuer_key="issuer-b",
        valid_from=date(2026, 1, 1),
        instrument_canonical_symbol="XNAS:TEST",
        instrument_valid_from=date(2025, 12, 31),
    )

    with pytest.raises(AmbiguousSymbolAliasError, match="AMBIGUOUS_SYMBOL_ALIAS"):
        validate_us_aliases([first, second])

    with pytest.raises(AmbiguousSymbolAliasError, match="AMBIGUOUS_SYMBOL_ALIAS"):
        resolve_us_alias_for_date(
            source_symbol="TEST",
            exchange="NASDAQ",
            aliases=[first, second],
            as_of=date(2026, 1, 1),
        )


def test_us_alias_lookup_rejects_symbols_outside_the_effective_mapping() -> None:
    alias = normalize_us_alias(
        source_symbol="AAPL",
        exchange="NASDAQ",
        issuer_key="cik0000320193",
        valid_from=date(2026, 1, 1),
    )

    with pytest.raises(SymbolNormalizationError, match="SYMBOL_UNRESOLVED"):
        resolve_us_alias_for_date(
            source_symbol="UNKNOWN",
            exchange="NASDAQ",
            aliases=[alias],
            as_of=date(2026, 1, 1),
        )


@pytest.mark.parametrize("_case_id", [pytest.param("TIME-001", id="TIME-001")])
def test_time_001_to_us_market_utc_converts_aware_datetime(_case_id: str) -> None:
    value = datetime(2026, 7, 23, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert to_us_market_utc(value) == datetime(2026, 7, 23, 1, 30, tzinfo=UTC)


@pytest.mark.parametrize("_case_id", [pytest.param("TIME-002", id="TIME-002")])
def test_time_002_to_us_market_utc_rejects_naive_datetime(_case_id: str) -> None:
    with pytest.raises(TimezoneRequiredError):
        to_us_market_utc(datetime(2026, 7, 23, 9, 30))


@pytest.mark.parametrize("_case_id", [pytest.param("TIME-005", id="TIME-005")])
def test_time_005_us_dst_uses_iana_timezone_not_fixed_offset(_case_id: str) -> None:
    summer_open = datetime(2026, 7, 23, 9, 30, tzinfo=ZoneInfo("America/New_York"))

    assert to_us_market_utc(summer_open) == datetime(2026, 7, 23, 13, 30, tzinfo=UTC)


@pytest.mark.parametrize("_case_id", [pytest.param("TIME-006", id="TIME-006")])
def test_time_006_rejects_nonexistent_new_york_local_time(_case_id: str) -> None:
    nonexistent = datetime(2026, 3, 8, 2, 30, tzinfo=ZoneInfo("America/New_York"))

    with pytest.raises(NonexistentLocalTimeError, match="NONEXISTENT_LOCAL_TIME"):
        to_us_market_utc(nonexistent)


@pytest.mark.parametrize("_case_id", [pytest.param("TIME-010", id="TIME-010")])
def test_time_010_trading_date_uses_new_york_market_day(_case_id: str) -> None:
    after_utc_midnight = datetime(2026, 7, 24, 1, 0, tzinfo=UTC)

    assert us_trading_date(after_utc_midnight) == date(2026, 7, 23)


def test_us_half_day_session_uses_calendar_lookup_and_new_york_dst_rules() -> None:
    session = us_equity_session_window(date(2026, 11, 27))

    calendar_day = us_equity_calendar_day(date(2026, 11, 27))
    assert calendar_day.status == "early_close"
    assert calendar_day.calendar_version == US_EQUITY_CALENDAR_VERSION
    assert session.open_at == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert session.close_at == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    assert session.trading_date == date(2026, 11, 27)
    assert session.calendar_version == US_EQUITY_CALENDAR_VERSION
    assert session.early_close


def test_us_christmas_eve_2026_is_half_day() -> None:
    session = us_equity_session_window(date(2026, 12, 24))

    assert us_equity_calendar_day(date(2026, 12, 24)).status == "early_close"
    assert session.close_at == datetime(2026, 12, 24, 18, 0, tzinfo=UTC)


def test_us_regular_session_uses_new_york_dst_rules() -> None:
    session = us_equity_session_window(date(2026, 1, 5))

    assert session.open_at == datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    assert session.close_at == datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
    assert not session.early_close


def test_us_closed_market_day_has_no_session() -> None:
    closed_days = [
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    ]
    assert [us_equity_calendar_day(day).status for day in closed_days] == [
        "closed",
    ] * len(closed_days)

    with pytest.raises(UsMarketClosedError, match="MARKET_CLOSED"):
        us_equity_session_window(date(2026, 7, 3))


def test_us_calendar_rejects_years_without_a_versioned_snapshot() -> None:
    with pytest.raises(UsCalendarUnavailableError, match="CALENDAR_UNAVAILABLE"):
        us_equity_calendar_day(date(2027, 1, 4))


@pytest.mark.parametrize("case", load_fixture("value_cases.json"), ids=fixture_case_id)
def test_unit_001_002_004_005_us_values_normalize_without_float(
    case: JsonFixtureCase,
) -> None:
    scale = Decimal(str(case["scale"])) if isinstance(case["scale"], str) else None
    actual = normalize_us_value(
        raw_value=str(case["raw_value"]),
        unit_hint=case["unit_hint"] if isinstance(case["unit_hint"], str) else None,
        scale=scale,
    )

    assert actual.value == Decimal(str(case["expected_value"]))
    assert str(actual.value) == case["expected_value"]
    assert actual.unit == case["expected_unit"]
    assert actual.currency == case["expected_currency"]
    assert actual.raw_unit == case["expected_raw_unit"]


def test_us_value_normalizes_plural_basis_points() -> None:
    actual = normalize_us_value("12.5bps")

    assert actual.value == Decimal("0.125")
    assert actual.unit == "percent"
    assert actual.raw_unit == "bps"


def test_unit_006_numeric_us_value_without_unit_hint_is_rejected() -> None:
    with pytest.raises(UnitNormalizationError, match="UNIT_REQUIRED"):
        normalize_us_value("42")


@pytest.mark.parametrize("_case_id", [pytest.param("UNIT-007", id="UNIT-007")])
def test_unit_007_us_value_preserves_decimal_precision_for_rate_unit(_case_id: str) -> None:
    actual = normalize_us_value("0.100000000000000001", unit_hint="rate")

    assert actual.value == Decimal("0.100000000000000001")
    assert actual.unit == "rate"
    assert actual.currency is None


def test_us_value_normalize_is_deterministic() -> None:
    assert normalize_us_value("25bp") == normalize_us_value("25bp")
    assert normalize_us_value("25bp") == normalize_us_value("0.25", unit_hint="percent")


def test_us_value_rejects_unknown_unit_hint() -> None:
    with pytest.raises(UnitNormalizationError, match="UNKNOWN_UNIT"):
        normalize_us_value("7", unit_hint="ratio")

    with pytest.raises(UnitNormalizationError, match="UNKNOWN_UNIT"):
        normalize_us_value("7", unit_hint="EUR")


def test_us_value_rejects_unparseable_decimal() -> None:
    with pytest.raises(UnitNormalizationError, match="UNIT_NORMALIZATION_ERROR"):
        normalize_us_value("not-a-number", unit_hint="percent")


@pytest.mark.parametrize(
    "sentinel",
    [
        pytest.param("--", id="UNIT-009-dash"),
        pytest.param("N/A", id="UNIT-009-na"),
        pytest.param("", id="UNIT-009-empty"),
    ],
)
def test_unit_009_missing_us_values_stay_null_not_zero(sentinel: str) -> None:
    actual = normalize_us_value(sentinel, unit_hint="percent")

    assert actual.value is None
    assert actual.unit == "percent"
    assert actual.missing_reason == "not_reported"


def test_unit_009_missing_us_value_without_unit_hint_is_rejected() -> None:
    with pytest.raises(UnitNormalizationError, match="UNIT_REQUIRED"):
        normalize_us_value("--")

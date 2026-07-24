"""CN/HK public-semantics normalization.

This package freezes only the symbol, calendar, time, and unit semantics that
Issue #3 is allowed to own. Upstream endpoint field meanings remain pending
the #1 source review, so provider adapters should call these helpers before
building existing public contracts instead of adding region DTOs.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from zoneinfo import ZoneInfo

from macro_platform.contracts.common import Region
from macro_platform.normalization.common import TimezoneRequiredError, to_utc

CN_VENUES = frozenset({"XSHG", "XSHE"})
HK_VENUES = frozenset({"XHKG"})
CN_TIMEZONE = "Asia/Shanghai"
HK_TIMEZONE = "Asia/Hong_Kong"


class MappingErrorCode(StrEnum):
    SYMBOL = "symbol_mapping"
    TIME = "time_mapping"
    TRADING_CALENDAR = "trading_calendar_mapping"
    UNIT = "unit_mapping"


class CnHkNormalizationError(ValueError):
    """Base error for provider-visible CN/HK normalization failures."""

    def __init__(self, code: MappingErrorCode, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


class SymbolMappingError(CnHkNormalizationError):
    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(MappingErrorCode.SYMBOL, message, field=field)


class TimeMappingError(CnHkNormalizationError):
    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(MappingErrorCode.TIME, message, field=field)


class TradingCalendarMappingError(CnHkNormalizationError):
    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(MappingErrorCode.TRADING_CALENDAR, message, field=field)


class UnitMappingError(CnHkNormalizationError):
    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(MappingErrorCode.UNIT, message, field=field)


@dataclass(frozen=True)
class AliasEffectiveDate:
    instrument_key: str
    provider_id: str
    source_symbol: str
    canonical_symbol: str
    valid_from: date
    valid_to: date | None = None


@dataclass(frozen=True)
class InstrumentAliasRegistryEntry:
    instrument_key: str
    provider_id: str
    source_symbol: str
    venue_mic: str
    valid_from: date
    valid_to: date | None = None


@dataclass(frozen=True)
class NormalizedInstrumentSymbol:
    instrument_id: str
    instrument_key: str
    canonical_symbol: str
    region: Region
    venue_mic: str
    local_symbol: str
    timezone: str
    aliases: tuple[AliasEffectiveDate, ...]


@dataclass(frozen=True)
class NormalizedTimestamp:
    utc: datetime
    local: datetime
    local_trading_date: date
    timezone: str


@dataclass(frozen=True)
class NormalizedUnit:
    value: Decimal
    unit: str
    source_unit: str
    scale: Decimal
    currency: str | None = None
    is_percent: bool = False


@dataclass(frozen=True)
class MissingUnitValue:
    value: None
    unit: str
    source_unit: str
    missing_reason: str
    currency: str | None = None


def normalize_instrument_symbol(
    *,
    region: Region,
    venue_mic: str,
    local_symbol: str,
    valid_from: date,
    provider_id: str,
    instrument_key: str,
    valid_to: date | None = None,
) -> NormalizedInstrumentSymbol:
    """Normalize exchange-specific symbols without guessing the exchange."""

    clean_venue = venue_mic.strip().upper()
    clean_symbol = local_symbol.strip()
    source_symbol = clean_symbol
    clean_provider_id = provider_id.strip()
    clean_instrument_key = instrument_key.strip()
    if not clean_provider_id:
        raise SymbolMappingError("provider_id is required", field="provider_id")
    if not clean_instrument_key:
        raise SymbolMappingError("instrument_key is required", field="instrument_key")
    if valid_to is not None and valid_to <= valid_from:
        raise SymbolMappingError("valid_to must be later than valid_from", field="valid_to")

    parsed_venue, parsed_symbol = _split_prefixed_symbol(clean_symbol)
    if parsed_venue is not None:
        if clean_venue and clean_venue != parsed_venue:
            raise SymbolMappingError("symbol venue conflicts with venue_mic", field="local_symbol")
        clean_venue = parsed_venue
        clean_symbol = parsed_symbol

    if region is Region.CN:
        normalized_symbol = _normalize_cn_symbol(clean_venue, clean_symbol)
        timezone = CN_TIMEZONE
    elif region is Region.HK:
        normalized_symbol = _normalize_hk_symbol(clean_venue, clean_symbol)
        timezone = HK_TIMEZONE
    else:
        raise SymbolMappingError("only CN/HK regions are supported", field="region")

    canonical_symbol = f"{clean_venue}:{normalized_symbol}"
    return NormalizedInstrumentSymbol(
        instrument_id=_instrument_id(region, clean_instrument_key),
        instrument_key=clean_instrument_key,
        canonical_symbol=canonical_symbol,
        region=region,
        venue_mic=clean_venue,
        local_symbol=normalized_symbol,
        timezone=timezone,
        aliases=(
            AliasEffectiveDate(
                instrument_key=clean_instrument_key,
                provider_id=clean_provider_id,
                source_symbol=source_symbol,
                canonical_symbol=canonical_symbol,
                valid_from=valid_from,
                valid_to=valid_to,
            ),
        ),
    )


def resolve_instrument_alias(
    *,
    region: Region,
    venue_mic: str,
    local_symbol: str,
    as_of: date,
    provider_id: str,
    aliases: tuple[InstrumentAliasRegistryEntry, ...],
    include_inactive: bool = False,
) -> NormalizedInstrumentSymbol:
    clean_provider_id = provider_id.strip()
    if not clean_provider_id:
        raise SymbolMappingError("provider_id is required", field="provider_id")
    clean_venue = venue_mic.strip().upper()
    clean_symbol = local_symbol.strip()
    parsed_venue, parsed_symbol = _split_prefixed_symbol(clean_symbol)
    if parsed_venue is not None:
        if clean_venue and clean_venue != parsed_venue:
            raise SymbolMappingError("symbol venue conflicts with venue_mic", field="local_symbol")
        clean_venue = parsed_venue
        clean_symbol = parsed_symbol

    requested_symbol = _normalize_symbol_for_region(region, clean_venue, clean_symbol)
    matching = tuple(
        entry
        for entry in aliases
        if entry.provider_id.strip() == clean_provider_id
        and _alias_matches(region, entry, clean_venue, requested_symbol)
    )
    active = tuple(entry for entry in matching if _alias_active(entry, as_of))
    candidates = active or (matching if include_inactive else ())
    if not candidates:
        if matching:
            raise SymbolMappingError("inactive symbol alias", field="local_symbol")
        raise SymbolMappingError("symbol alias unresolved", field="local_symbol")
    instrument_keys = {entry.instrument_key for entry in candidates}
    if len(instrument_keys) != 1:
        raise SymbolMappingError("ambiguous symbol alias", field="local_symbol")
    entry = candidates[0]
    return normalize_instrument_symbol(
        region=region,
        venue_mic=entry.venue_mic,
        local_symbol=entry.source_symbol,
        valid_from=entry.valid_from,
        valid_to=entry.valid_to,
        provider_id=clean_provider_id,
        instrument_key=entry.instrument_key,
    )


def normalize_timestamp(
    *,
    region: Region,
    value: datetime,
    trading_date: date | None = None,
) -> NormalizedTimestamp:
    timezone = _timezone_for_region(region)
    try:
        utc_value = to_utc(value)
    except TimezoneRequiredError as exc:
        raise TimeMappingError("datetime must include a timezone", field="value") from exc
    local_value = utc_value.astimezone(ZoneInfo(timezone))
    return NormalizedTimestamp(
        utc=utc_value,
        local=local_value,
        local_trading_date=trading_date or local_value.date(),
        timezone=timezone,
    )


def local_session_datetime(*, region: Region, trading_day: date, session_time: time) -> datetime:
    timezone = _timezone_for_region(region)
    if session_time.tzinfo is not None and session_time.utcoffset() is not None:
        raise TimeMappingError(
            "session_time must be local wall time without timezone", field="time"
        )
    return datetime.combine(trading_day, session_time, tzinfo=ZoneInfo(timezone))


def normalize_trading_date(
    *,
    region: Region,
    value: date | datetime,
    holidays: frozenset[date] = frozenset(),
) -> date:
    """Validate a local trading date against the fixture-backed calendar scaffold.

    Real exchange holiday and half-day rules are intentionally pending #1 review.
    Providers can pass reviewed holiday sets later without changing this public
    call shape.
    """

    if isinstance(value, datetime):
        normalized = normalize_timestamp(region=region, value=value).local_trading_date
    else:
        normalized = value
    if not is_trading_day(region=region, trading_day=normalized, holidays=holidays):
        raise TradingCalendarMappingError("date is not a trading day", field="trading_date")
    return normalized


def is_trading_day(
    *,
    region: Region,
    trading_day: date,
    holidays: frozenset[date] = frozenset(),
) -> bool:
    _timezone_for_region(region)
    return trading_day.weekday() < 5 and trading_day not in holidays


def normalize_decimal(value: Decimal | int | str) -> Decimal:
    if isinstance(value, bool):
        raise UnitMappingError("boolean is not a valid decimal value", field="value")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    try:
        return Decimal(value.strip())
    except (AttributeError, InvalidOperation) as exc:
        raise UnitMappingError(
            "decimal values must be Decimal, int, or string", field="value"
        ) from exc


def normalize_unit(
    *, region: Region, value: Decimal | int | str, source_unit: str
) -> NormalizedUnit:
    decimal_value = normalize_decimal(value)
    clean_unit = " ".join(source_unit.strip().split())
    if not clean_unit:
        raise UnitMappingError("source_unit is required", field="source_unit")
    unit_key = clean_unit.lower()
    if unit_key in _PERCENT_UNITS:
        return NormalizedUnit(
            value=decimal_value,
            unit="percent",
            source_unit=clean_unit,
            scale=Decimal("1"),
            is_percent=True,
        )
    if unit_key in _BASIS_POINT_UNITS:
        return NormalizedUnit(
            value=decimal_value / Decimal("100"),
            unit="percent",
            source_unit=clean_unit,
            scale=Decimal("0.01"),
            is_percent=True,
        )

    unit, currency, scale = _currency_unit(region, unit_key)
    return NormalizedUnit(
        value=decimal_value * scale,
        unit=unit,
        source_unit=clean_unit,
        scale=scale,
        currency=currency,
    )


def normalize_optional_unit(
    *, region: Region, value: Decimal | int | str | None, source_unit: str
) -> NormalizedUnit | MissingUnitValue:
    clean_unit = " ".join(source_unit.strip().split())
    if not clean_unit:
        raise UnitMappingError("source_unit is required", field="source_unit")
    if value is None or (isinstance(value, str) and value.strip() in _MISSING_VALUE_SENTINELS):
        unit_key = clean_unit.lower()
        if unit_key in _PERCENT_UNITS or unit_key in _BASIS_POINT_UNITS:
            return MissingUnitValue(
                value=None,
                unit="percent",
                source_unit=clean_unit,
                missing_reason="vendor_missing",
            )
        unit, currency, _scale = _currency_unit(region, unit_key)
        return MissingUnitValue(
            value=None,
            unit=unit,
            source_unit=clean_unit,
            missing_reason="vendor_missing",
            currency=currency,
        )
    return normalize_unit(region=region, value=value, source_unit=source_unit)


def _normalize_cn_symbol(venue_mic: str, local_symbol: str) -> str:
    if venue_mic not in CN_VENUES:
        raise SymbolMappingError("CN venue must be XSHG or XSHE", field="venue_mic")
    if not (len(local_symbol) == 6 and local_symbol.isdecimal()):
        raise SymbolMappingError("CN local_symbol must be a six-digit code", field="local_symbol")
    return local_symbol


def _normalize_hk_symbol(venue_mic: str, local_symbol: str) -> str:
    if venue_mic not in HK_VENUES:
        raise SymbolMappingError("HK venue must be XHKG", field="venue_mic")
    if not (1 <= len(local_symbol) <= 5 and local_symbol.isdecimal()):
        raise SymbolMappingError("HK local_symbol must be one to five digits", field="local_symbol")
    return local_symbol.zfill(5)


def _instrument_id(region: Region, instrument_key: str) -> str:
    seed = f"{region.value}|{instrument_key}"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    token = base64.b32encode(digest).decode("ascii").lower().rstrip("=")[:26]
    return f"ins_{token}"


def _split_prefixed_symbol(source_symbol: str) -> tuple[str | None, str]:
    upper_symbol = source_symbol.upper()
    if ":" in upper_symbol:
        venue, _, symbol = upper_symbol.partition(":")
        return venue, symbol
    if upper_symbol.startswith("SH") and len(upper_symbol) == 8:
        return "XSHG", upper_symbol[2:]
    if upper_symbol.startswith("SZ") and len(upper_symbol) == 8:
        return "XSHE", upper_symbol[2:]
    if upper_symbol.endswith(".SH") or upper_symbol.endswith(".SS"):
        return "XSHG", upper_symbol[:-3]
    if upper_symbol.endswith(".SZ"):
        return "XSHE", upper_symbol[:-3]
    if upper_symbol.endswith(".HK"):
        return "XHKG", upper_symbol[:-3]
    return None, source_symbol


def _normalize_symbol_for_region(region: Region, venue_mic: str, source_symbol: str) -> str:
    if region is Region.CN:
        return _normalize_cn_symbol(venue_mic, source_symbol)
    if region is Region.HK:
        return _normalize_hk_symbol(venue_mic, source_symbol)
    raise SymbolMappingError("only CN/HK regions are supported", field="region")


def _alias_matches(
    region: Region, entry: InstrumentAliasRegistryEntry, venue_mic: str, normalized_symbol: str
) -> bool:
    entry_venue, entry_symbol = _split_prefixed_symbol(entry.source_symbol)
    candidate_venue = entry_venue or entry.venue_mic.strip().upper()
    if candidate_venue != venue_mic:
        return False
    try:
        candidate_symbol = _normalize_symbol_for_region(region, candidate_venue, entry_symbol)
    except SymbolMappingError:
        return False
    return candidate_symbol == normalized_symbol


def _alias_active(entry: InstrumentAliasRegistryEntry, as_of: date) -> bool:
    return entry.valid_from <= as_of and (entry.valid_to is None or as_of < entry.valid_to)


def _timezone_for_region(region: Region) -> str:
    if region is Region.CN:
        return CN_TIMEZONE
    if region is Region.HK:
        return HK_TIMEZONE
    raise TimeMappingError("only CN/HK regions are supported", field="region")


def _currency_unit(region: Region, unit_key: str) -> tuple[str, str, Decimal]:
    if region is Region.CN:
        mapping = _CN_CURRENCY_UNITS
    elif region is Region.HK:
        mapping = _HK_CURRENCY_UNITS
    else:
        raise UnitMappingError("only CN/HK regions are supported", field="region")
    try:
        currency, scale = mapping[unit_key]
    except KeyError as exc:
        raise UnitMappingError("unsupported source unit", field="source_unit") from exc
    return currency, currency, scale


_PERCENT_UNITS = frozenset({"%", "percent", "pct", "percentage", "百分比", "百分点"})
_BASIS_POINT_UNITS = frozenset({"bp", "bps", "basis point", "basis points", "基点"})
_MISSING_VALUE_SENTINELS = frozenset({"", "--", "N/A", "n/a", "NA", "na"})
_CN_CURRENCY_UNITS: dict[str, tuple[str, Decimal]] = {
    "cny": ("CNY", Decimal("1")),
    "rmb": ("CNY", Decimal("1")),
    "人民币": ("CNY", Decimal("1")),
    "元": ("CNY", Decimal("1")),
    "万元": ("CNY", Decimal("10000")),
    "亿元": ("CNY", Decimal("100000000")),
}
_HK_CURRENCY_UNITS: dict[str, tuple[str, Decimal]] = {
    "hkd": ("HKD", Decimal("1")),
    "hk$": ("HKD", Decimal("1")),
    "港元": ("HKD", Decimal("1")),
    "hkd mn": ("HKD", Decimal("1000000")),
    "hk$ mn": ("HKD", Decimal("1000000")),
    "hkd million": ("HKD", Decimal("1000000")),
    "million hkd": ("HKD", Decimal("1000000")),
    "百万港元": ("HKD", Decimal("1000000")),
}


__all__ = [
    "CN_TIMEZONE",
    "CN_VENUES",
    "HK_TIMEZONE",
    "HK_VENUES",
    "AliasEffectiveDate",
    "CnHkNormalizationError",
    "InstrumentAliasRegistryEntry",
    "MappingErrorCode",
    "MissingUnitValue",
    "NormalizedInstrumentSymbol",
    "NormalizedTimestamp",
    "NormalizedUnit",
    "SymbolMappingError",
    "TimeMappingError",
    "TradingCalendarMappingError",
    "UnitMappingError",
    "is_trading_day",
    "local_session_datetime",
    "normalize_decimal",
    "normalize_instrument_symbol",
    "normalize_optional_unit",
    "normalize_timestamp",
    "normalize_trading_date",
    "normalize_unit",
    "resolve_instrument_alias",
]

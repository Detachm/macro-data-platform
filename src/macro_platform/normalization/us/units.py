from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final

from macro_platform.normalization.us.errors import UnitNormalizationError

_MISSING_SENTINELS: Final = frozenset({"", "--", "N/A", "NA", "NULL"})
_UNIT_HINTS: Final = frozenset({"percent", "index_point", "rate"})


@dataclass(frozen=True)
class _NormalizedUsValue:
    raw_value: str = field(compare=False)
    value: Decimal | None
    unit: str
    currency: str | None
    raw_unit: str | None = field(compare=False)
    normalization_rule: str = field(compare=False)
    missing_reason: str | None = None


def normalize_us_value(
    raw_value: str,
    *,
    unit_hint: str | None = None,
    scale: Decimal | None = None,
) -> _NormalizedUsValue:
    raw = raw_value.strip()
    if raw.upper() in _MISSING_SENTINELS:
        unit, currency = _resolve_unit(unit_hint, raw)
        return _NormalizedUsValue(
            raw_value=raw_value,
            value=None,
            unit=unit,
            currency=currency,
            raw_unit=unit_hint,
            normalization_rule="missing_sentinel_to_null",
            missing_reason="not_reported",
        )

    numeric_text = raw
    raw_unit = unit_hint
    normalization_rule = "decimal_from_text"
    if raw.endswith("%"):
        numeric_text = raw[:-1].strip()
        raw_unit = "%"
        unit = "percent"
        currency = None
        normalization_rule = "percent_suffix"
    elif (basis_points_suffix := _basis_points_suffix(raw)) is not None:
        numeric_text = raw[: -len(basis_points_suffix)].strip()
        raw_unit = basis_points_suffix
        unit = "percent"
        currency = None
        normalization_rule = "basis_points_to_percent"
    else:
        unit, currency = _resolve_unit(unit_hint, raw)

    try:
        value = Decimal(numeric_text)
    except InvalidOperation as exc:
        raise UnitNormalizationError(f"cannot parse decimal value: {raw_value}") from exc

    if normalization_rule == "basis_points_to_percent":
        value = value / Decimal("100")
    if scale is not None:
        value *= scale
        normalization_rule = f"{normalization_rule}_scaled_by_{scale}"
    value = _canonical_decimal(value)

    return _NormalizedUsValue(
        raw_value=raw_value,
        value=value,
        unit=unit,
        currency=currency,
        raw_unit=raw_unit,
        normalization_rule=normalization_rule,
    )


def _resolve_unit(unit_hint: str | None, raw_value: str) -> tuple[str, str | None]:
    if unit_hint is None:
        if raw_value.endswith("%") or raw_value.lower().endswith(("bp", "bps")):
            return "percent", None
        raise UnitNormalizationError(
            "unit hint is required for numeric values without an inline unit",
            code="UNIT_REQUIRED",
        )

    normalized = unit_hint.strip()
    upper = normalized.upper()
    if upper == "USD":
        return "currency", "USD"
    if normalized in _UNIT_HINTS:
        return normalized, None
    raise UnitNormalizationError(f"unsupported unit hint: {unit_hint}", code="UNKNOWN_UNIT")


def _basis_points_suffix(raw_value: str) -> str | None:
    normalized = raw_value.lower()
    if normalized.endswith("bps"):
        return raw_value[-3:]
    if normalized.endswith("bp"):
        return raw_value[-2:]
    return None


def _canonical_decimal(value: Decimal) -> Decimal:
    if value == value.to_integral_value():
        return value.quantize(Decimal("1"))
    return value.normalize()

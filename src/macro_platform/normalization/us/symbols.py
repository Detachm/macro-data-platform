from __future__ import annotations

import hashlib
from collections.abc import Container
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from macro_platform.normalization.us.errors import (
    AmbiguousSymbolAliasError,
    SymbolNormalizationError,
    UnsupportedExchangeError,
)

_EXCHANGE_TO_MIC: Final[dict[str, str]] = {
    "NASDAQ": "XNAS",
    "NAS": "XNAS",
    "XNAS": "XNAS",
    "NYSE": "XNYS",
    "XNYS": "XNYS",
    "NYSE ARCA": "ARCX",
    "ARCA": "ARCX",
    "ARCX": "ARCX",
    "NYSE AMERICAN": "XASE",
    "AMEX": "XASE",
    "XASE": "XASE",
    "CBOE": "BATS",
    "BATS": "BATS",
}
_KNOWN_US_MICS: Final = frozenset(_EXCHANGE_TO_MIC.values())


@dataclass(frozen=True)
class _CanonicalSymbolParts:
    source_symbol: str = field(compare=False)
    venue_mic: str
    local_symbol: str
    canonical_symbol: str


@dataclass(frozen=True)
class _NormalizedUsSymbol:
    source_symbol: str = field(compare=False)
    local_symbol: str
    venue_mic: str
    canonical_symbol: str


@dataclass(frozen=True)
class UsInstrumentIdentity:
    """Immutable root identity established when an instrument is first onboarded."""

    issuer_key: str
    first_canonical_symbol: str
    first_valid_from: date


@dataclass(frozen=True)
class _UsAlias:
    source_symbol: str
    local_symbol: str
    venue_mic: str
    canonical_symbol: str
    instrument_id: str
    valid_from: date
    valid_to: date | None
    issuer_key: str | None


def normalize_us_symbol(source_symbol: str, exchange: str | None = None) -> _NormalizedUsSymbol:
    source = source_symbol.strip()
    if not source:
        raise SymbolNormalizationError("source symbol is empty")

    if ":" in source:
        canonical = _normalize_canonical_symbol_parts(source, allowed_mics=_KNOWN_US_MICS)
        venue_mic = canonical.venue_mic
        local_symbol = canonical.local_symbol
        if exchange is not None and _resolve_mic(exchange) != venue_mic:
            raise AmbiguousSymbolAliasError(
                f"canonical symbol {source} conflicts with exchange {exchange}"
            )
    else:
        if exchange is None:
            raise SymbolNormalizationError("exchange is required; refusing to guess US venue")
        venue_mic = _resolve_mic(exchange)
        local_symbol = _normalize_local_symbol(source)

    canonical_symbol = f"{venue_mic}:{local_symbol}"
    return _NormalizedUsSymbol(
        source_symbol=source_symbol,
        local_symbol=local_symbol,
        venue_mic=venue_mic,
        canonical_symbol=canonical_symbol,
    )


def normalize_us_alias(
    *,
    source_symbol: str,
    exchange: str,
    valid_from: date,
    instrument_identity: UsInstrumentIdentity,
    valid_to: date | None = None,
) -> _UsAlias:
    if valid_to is not None and valid_to <= valid_from:
        raise SymbolNormalizationError("valid_to must be later than valid_from")

    symbol = normalize_us_symbol(source_symbol, exchange=exchange)

    return _UsAlias(
        source_symbol=source_symbol,
        local_symbol=symbol.local_symbol,
        venue_mic=symbol.venue_mic,
        canonical_symbol=symbol.canonical_symbol,
        instrument_id=us_instrument_id(instrument_identity),
        valid_from=valid_from,
        valid_to=valid_to,
        issuer_key=_normalize_issuer_key(instrument_identity.issuer_key),
    )


def validate_us_aliases(aliases: list[_UsAlias]) -> None:
    for index, left in enumerate(aliases):
        for right in aliases[index + 1 :]:
            if (left.venue_mic, left.local_symbol) != (right.venue_mic, right.local_symbol):
                continue
            if left.instrument_id == right.instrument_id:
                continue
            if _date_ranges_overlap(
                left.valid_from, left.valid_to, right.valid_from, right.valid_to
            ):
                raise AmbiguousSymbolAliasError(
                    f"{left.canonical_symbol} maps to multiple instruments on overlapping dates"
                )


def resolve_us_alias_for_date(
    *,
    source_symbol: str,
    exchange: str,
    aliases: list[_UsAlias],
    as_of: date,
) -> _UsAlias:
    symbol = normalize_us_symbol(source_symbol, exchange=exchange)
    matches = [
        alias
        for alias in aliases
        if (alias.venue_mic, alias.local_symbol) == (symbol.venue_mic, symbol.local_symbol)
        and alias.valid_from <= as_of
        and (alias.valid_to is None or as_of < alias.valid_to)
    ]
    if not matches:
        raise SymbolNormalizationError(
            f"no effective alias mapping for {symbol.canonical_symbol} on {as_of.isoformat()}"
        )

    instrument_ids = {alias.instrument_id for alias in matches}
    if len(instrument_ids) > 1:
        raise AmbiguousSymbolAliasError(
            f"{symbol.canonical_symbol} maps to multiple instruments on {as_of.isoformat()}"
        )

    return max(matches, key=lambda alias: alias.valid_from)


def us_instrument_id(identity: UsInstrumentIdentity) -> str:
    """Create an immutable ID from an instrument's first-known identity.

    The current alias must never participate in this seed: a rename changes an
    alias, not the underlying instrument. `first_canonical_symbol` keeps share
    classes of the same issuer distinct while `issuer_key` ties provider aliases
    back to the issuer identity used during onboarding.
    """

    issuer_key = _normalize_issuer_key(identity.issuer_key)
    canonical_symbol = _normalize_id_canonical_symbol(identity.first_canonical_symbol)
    seed = "\x1f".join((issuer_key, canonical_symbol, identity.first_valid_from.isoformat()))
    return _short_stable_id("ins_us", seed)


def _normalize_canonical_symbol_parts(
    source_symbol: str,
    *,
    allowed_mics: Container[str] | None = None,
) -> _CanonicalSymbolParts:
    source = source_symbol.strip()
    if ":" not in source:
        raise SymbolNormalizationError("canonical symbol requires '<MIC>:<LOCAL_SYMBOL>'")

    venue_mic, raw_local_symbol = source.split(":", maxsplit=1)
    venue_mic = venue_mic.strip().upper()
    local_symbol = _normalize_local_symbol(raw_local_symbol)

    if not venue_mic:
        raise UnsupportedExchangeError("canonical symbol MIC is empty")
    if allowed_mics is not None and venue_mic not in allowed_mics:
        raise UnsupportedExchangeError(f"unsupported MIC: {venue_mic}")

    return _CanonicalSymbolParts(
        source_symbol=source_symbol,
        venue_mic=venue_mic,
        local_symbol=local_symbol,
        canonical_symbol=f"{venue_mic}:{local_symbol}",
    )


def _normalize_local_symbol(source: str) -> str:
    local_symbol = source.strip().upper()
    if not local_symbol:
        raise SymbolNormalizationError("source symbol is empty")
    return local_symbol


def _resolve_mic(exchange: str) -> str:
    normalized = " ".join(exchange.strip().upper().replace("_", " ").replace("-", " ").split())
    mic = _EXCHANGE_TO_MIC.get(normalized)
    if mic is None:
        raise UnsupportedExchangeError(f"unsupported US exchange: {exchange}")
    return mic


def _normalize_id_canonical_symbol(canonical_symbol: str) -> str:
    return _normalize_canonical_symbol_parts(
        canonical_symbol,
        allowed_mics=_KNOWN_US_MICS,
    ).canonical_symbol


def _normalize_issuer_key(issuer_key: str) -> str:
    normalized = issuer_key.strip()
    if not normalized:
        raise SymbolNormalizationError("issuer key is required for stable instrument identity")
    return normalized


def _short_stable_id(namespace: str, seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"{namespace}_{digest}"


def _date_ranges_overlap(
    left_from: date,
    left_to: date | None,
    right_from: date,
    right_to: date | None,
) -> bool:
    left_end = left_to or date.max
    right_end = right_to or date.max
    return left_from < right_end and right_from < left_end

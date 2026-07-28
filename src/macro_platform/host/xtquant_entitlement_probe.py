"""Discover approved HK index source symbols without printing paid market payloads."""

from __future__ import annotations

import argparse
import importlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from types import ModuleType
from typing import Any, Literal, Protocol

TargetIndex = Literal["hang_seng_index", "hang_seng_tech_index"]


class XtQuantMetadataClient(Protocol):
    def connect(self, ip: str, port: int) -> object: ...

    def download_sector_data(self) -> None: ...

    def get_sector_list(self) -> list[str]: ...

    def get_stock_list_in_sector(self, sector_name: str) -> list[str]: ...

    def get_instrument_detail(
        self, stock_code: str, iscomplete: bool = False
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class IndexIdentityMatch:
    target: TargetIndex
    source_symbol: str
    instrument_name: str
    exchange_id: str | None
    product_type: str | int | None


@dataclass(frozen=True, slots=True)
class EntitlementProbeResult:
    status: Literal["confirmed", "incomplete", "ambiguous"]
    sectors_scanned: int
    symbols_scanned: int
    matches: tuple[IndexIdentityMatch, ...]
    missing_targets: tuple[TargetIndex, ...]
    ambiguous_targets: tuple[TargetIndex, ...]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Confirm HSI and HSTECH XtQuant symbols from instrument metadata"
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=58615)
    parser.add_argument("--sector", action="append", dest="sectors")
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    client = _load_metadata_client()
    client.connect(arguments.host, arguments.port)
    result = probe_hk_index_entitlements(client, sectors=arguments.sectors)
    print(json.dumps(asdict(result), ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(0 if result.status == "confirmed" else 1)


def probe_hk_index_entitlements(
    client: XtQuantMetadataClient,
    *,
    sectors: list[str] | None = None,
) -> EntitlementProbeResult:
    client.download_sector_data()
    available_sectors = client.get_sector_list()
    selected_sectors = tuple(sectors) if sectors else _select_hk_index_sectors(available_sectors)
    symbols = sorted(
        {
            symbol
            for sector in selected_sectors
            for symbol in client.get_stock_list_in_sector(sector)
        }
    )
    matches: list[IndexIdentityMatch] = []
    for symbol in symbols:
        detail = client.get_instrument_detail(symbol, iscomplete=False)
        if not detail:
            continue
        name = str(detail.get("InstrumentName") or detail.get("ProductName") or "").strip()
        target = _classify_exact_index_name(name)
        if target is None:
            continue
        matches.append(
            IndexIdentityMatch(
                target=target,
                source_symbol=symbol,
                instrument_name=name,
                exchange_id=_optional_string(detail.get("ExchangeID")),
                product_type=_optional_product_type(detail.get("ProductType")),
            )
        )

    required: tuple[TargetIndex, ...] = ("hang_seng_index", "hang_seng_tech_index")
    counts = {target: sum(match.target == target for match in matches) for target in required}
    missing = tuple(target for target in required if counts[target] == 0)
    ambiguous = tuple(target for target in required if counts[target] > 1)
    status: Literal["confirmed", "incomplete", "ambiguous"]
    if ambiguous:
        status = "ambiguous"
    elif missing:
        status = "incomplete"
    else:
        status = "confirmed"
    return EntitlementProbeResult(
        status=status,
        sectors_scanned=len(selected_sectors),
        symbols_scanned=len(symbols),
        matches=tuple(matches),
        missing_targets=missing,
        ambiguous_targets=ambiguous,
    )


def _select_hk_index_sectors(sectors: list[str]) -> tuple[str, ...]:
    selected: list[str] = []
    for sector in sectors:
        normalized = _normalize_name(sector)
        regional = any(
            marker in normalized for marker in ("港股", "香港", "恒生", "hongkong", "hangseng")
        )
        index_like = "指数" in normalized or "index" in normalized
        if regional and index_like:
            selected.append(sector)
    return tuple(selected)


def _classify_exact_index_name(name: str) -> TargetIndex | None:
    normalized = _normalize_name(name)
    if normalized in {
        "恒生科技指数",
        "恒生科技指数hstech",
        "hangsengtechindex",
        "hangsengtechindexhstech",
    }:
        return "hang_seng_tech_index"
    if normalized in {"恒生指数", "恒生指数hsi", "hangsengindex", "hangsengindexhsi"}:
        return "hang_seng_index"
    return None


def _normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", normalized)


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_product_type(value: object) -> str | int | None:
    if isinstance(value, str) or type(value) is int:
        return value
    return None


def _load_metadata_client() -> XtQuantMetadataClient:
    module: ModuleType = importlib.import_module("xtquant.xtdata")
    return module


__all__ = [
    "EntitlementProbeResult",
    "IndexIdentityMatch",
    "main",
    "probe_hk_index_entitlements",
]

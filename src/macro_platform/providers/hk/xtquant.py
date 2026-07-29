"""Allowlisted XtQuant adapter for HK daily equity and index bars.

The XtQuant data-centre is an externally managed local service.  This adapter
does not start it, configure credentials, or manipulate its port: it only
connects to the configured endpoint, downloads a bounded ``1d`` window, then
normalizes the returned data frames.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import Any, Final, Protocol, cast
from zoneinfo import ZoneInfo

from macro_platform.contracts.common import (
    AssetClass,
    AvailabilityBasis,
    Region,
    SourceRef,
    WarningItem,
)
from macro_platform.contracts.market import (
    Adjustment,
    BarQuery,
    Instrument,
    InstrumentStatus,
    Interval,
    MarketBar,
)
from macro_platform.contracts.provider import (
    Dataset,
    FetchContext,
    ProviderCapabilities,
    ProviderHealth,
    ProviderPage,
)
from macro_platform.normalization.common import canonical_json_checksum, stable_id
from macro_platform.providers.base import (
    ProviderAuthenticationError,
    ProviderCursorError,
    ProviderError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
)
from macro_platform.providers.registry import ProviderRegistry

HK_XTQUANT_PROVIDER_ID: Final = "hk.xtquant.v1"
HK_XTQUANT_PRIMARY_ROLE: Final = "hk.bars.primary"
HK_XTQUANT_EQUITY_ROLE: Final = "hk.equity-bars.supplemental"
_MAX_CURSOR_OFFSET: Final = 100_000
_MAX_QUERY_WINDOW: Final = timedelta(days=5 * 366)
_MAX_ROWS_PER_INSTRUMENT: Final = 2_500
_HK_TIMEZONE: Final = ZoneInfo("Asia/Hong_Kong")


class XtQuantClient(Protocol):
    """Small, injectable subset of ``xtquant.xtdata`` used by this adapter."""

    def connect(
        self, ip: str = "", port: int | None = None, remember_if_success: bool = True
    ) -> object: ...

    def download_history_data2(
        self,
        stock_list: list[str],
        period: str,
        start_time: str = "",
        end_time: str = "",
        callback: object | None = None,
    ) -> object: ...

    def get_market_data_ex(
        self,
        field_list: list[str] | None = None,
        stock_list: list[str] | None = None,
        period: str = "1d",
        start_time: str = "",
        end_time: str = "",
        count: int = -1,
        dividend_type: str = "none",
        fill_data: bool = True,
    ) -> Mapping[str, object]: ...

    def disconnect(self) -> None: ...


@dataclass(frozen=True, slots=True)
class HkXtQuantInstrument:
    """Reviewed platform identity for one XtQuant HK symbol."""

    instrument_id: str
    canonical_symbol: str
    source_symbol: str
    currency: str = "HKD"

    def __post_init__(self) -> None:
        metadata = _INSTRUMENT_METADATA.get(self.source_symbol)
        if not self.instrument_id.strip():
            raise ValueError("XtQuant instrument_id must not be empty")
        if metadata is None:
            raise ValueError("XtQuant symbol is not in the approved HK allowlist")
        if self.canonical_symbol != metadata.canonical_symbol:
            raise ValueError("XtQuant canonical_symbol does not match the approved mapping")
        if self.currency != metadata.currency:
            raise ValueError("XtQuant instrument currency does not match the approved mapping")


@dataclass(frozen=True, slots=True)
class _XtQuantInstrumentMetadata:
    canonical_symbol: str
    name: str
    listed_on: date
    asset_class: AssetClass = AssetClass.EQUITY
    currency: str = "HKD"


_INSTRUMENT_METADATA: Final = {
    "00700.HK": _XtQuantInstrumentMetadata(
        canonical_symbol="XHKG:00700", name="Tencent Holdings Limited", listed_on=date(2004, 6, 16)
    ),
    "09988.HK": _XtQuantInstrumentMetadata(
        canonical_symbol="XHKG:09988",
        name="Alibaba Group Holding Limited",
        listed_on=date(2019, 11, 26),
    ),
    "03690.HK": _XtQuantInstrumentMetadata(
        canonical_symbol="XHKG:03690", name="Meituan", listed_on=date(2018, 9, 20)
    ),
    "01810.HK": _XtQuantInstrumentMetadata(
        canonical_symbol="XHKG:01810", name="Xiaomi Corporation", listed_on=date(2018, 7, 9)
    ),
    "00941.HK": _XtQuantInstrumentMetadata(
        canonical_symbol="XHKG:00941", name="China Mobile Limited", listed_on=date(1997, 10, 23)
    ),
    "00005.HK": _XtQuantInstrumentMetadata(
        canonical_symbol="XHKG:00005", name="HSBC Holdings plc", listed_on=date(1991, 12, 31)
    ),
    "00388.HK": _XtQuantInstrumentMetadata(
        canonical_symbol="XHKG:00388",
        name="Hong Kong Exchanges and Clearing Limited",
        listed_on=date(2000, 6, 27),
    ),
    "01299.HK": _XtQuantInstrumentMetadata(
        canonical_symbol="XHKG:01299", name="AIA Group Limited", listed_on=date(2010, 10, 29)
    ),
    "02318.HK": _XtQuantInstrumentMetadata(
        canonical_symbol="XHKG:02318",
        name="Ping An Insurance (Group) Company of China, Ltd.",
        listed_on=date(2004, 6, 24),
    ),
    "09618.HK": _XtQuantInstrumentMetadata(
        canonical_symbol="XHKG:09618", name="JD.com, Inc.", listed_on=date(2020, 6, 18)
    ),
    "HSI.HK": _XtQuantInstrumentMetadata(
        canonical_symbol="XHKG:HSI",
        name="Hang Seng Index",
        listed_on=date(1969, 11, 24),
        asset_class=AssetClass.INDEX,
    ),
    "HSCEI.HK": _XtQuantInstrumentMetadata(
        canonical_symbol="XHKG:HSCEI",
        name="Hang Seng China Enterprises Index",
        listed_on=date(1994, 8, 8),
        asset_class=AssetClass.INDEX,
    ),
    "HSTECH.HK": _XtQuantInstrumentMetadata(
        canonical_symbol="XHKG:HSTECH",
        name="Hang Seng TECH Index",
        listed_on=date(2020, 7, 27),
        asset_class=AssetClass.INDEX,
    ),
}

HK_XTQUANT_DEFAULT_INSTRUMENTS: Final = tuple(
    HkXtQuantInstrument(
        instrument_id=(
            f"ins_hk_{metadata.asset_class.value}_{source_symbol.removesuffix('.HK').lower()}"
        ),
        canonical_symbol=metadata.canonical_symbol,
        source_symbol=source_symbol,
        currency=metadata.currency,
    )
    for source_symbol, metadata in _INSTRUMENT_METADATA.items()
)

HK_XTQUANT_CORE_INDEX_INSTRUMENTS: Final = tuple(
    instrument
    for instrument in HK_XTQUANT_DEFAULT_INSTRUMENTS
    if _INSTRUMENT_METADATA[instrument.source_symbol].asset_class is AssetClass.INDEX
)
HK_XTQUANT_EQUITY_INSTRUMENTS: Final = tuple(
    instrument
    for instrument in HK_XTQUANT_DEFAULT_INSTRUMENTS
    if _INSTRUMENT_METADATA[instrument.source_symbol].asset_class is AssetClass.EQUITY
)


def hk_xtquant_instruments_from_symbols(symbols: str) -> tuple[HkXtQuantInstrument, ...]:
    """Resolve a comma-separated deployment allowlist to reviewed identities."""

    requested_symbols = tuple(
        symbol.strip().upper() for symbol in symbols.split(",") if symbol.strip()
    )
    if not requested_symbols:
        raise ValueError("HK_XTQUANT_SYMBOLS must contain at least one approved symbol")
    if len(set(requested_symbols)) != len(requested_symbols):
        raise ValueError("HK_XTQUANT_SYMBOLS must not contain duplicate symbols")
    by_symbol = {
        instrument.source_symbol: instrument for instrument in HK_XTQUANT_DEFAULT_INSTRUMENTS
    }
    try:
        return tuple(by_symbol[symbol] for symbol in requested_symbols)
    except KeyError as error:
        raise ValueError(
            "HK_XTQUANT_SYMBOLS contains a symbol outside the approved allowlist"
        ) from error


class HkXtQuantDailyBarsProvider:
    """Fetch raw HK daily bars through an externally managed XtQuant endpoint."""

    provider_id: Final[str] = HK_XTQUANT_PROVIDER_ID
    source_name: Final[str] = "XtQuant"

    def __init__(
        self,
        *,
        instruments: Sequence[HkXtQuantInstrument] = HK_XTQUANT_DEFAULT_INSTRUMENTS,
        client: XtQuantClient | None = None,
        host: str = "127.0.0.1",
        port: int = 58615,
        timeout_seconds: float = 30,
        cursor_signing_secret: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("XtQuant timeout_seconds must be positive")
        if not cursor_signing_secret:
            raise ValueError("XtQuant cursor_signing_secret must not be empty")
        if not host.strip() or len(host) > 253:
            raise ValueError("XtQuant host must be a non-empty hostname or IP address")
        if port < 1 or port > 65535:
            raise ValueError("XtQuant port must be between 1 and 65535")
        if not instruments:
            raise ValueError("XtQuant requires at least one approved HK instrument")
        by_id = {instrument.instrument_id: instrument for instrument in instruments}
        by_symbol = {instrument.source_symbol: instrument for instrument in instruments}
        if len(by_id) != len(instruments) or len(by_symbol) != len(instruments):
            raise ValueError("XtQuant instruments and source symbols must be unique")
        self._instruments_by_id = by_id
        self._client = client
        self._host = host.strip()
        self._port = port
        self._timeout_seconds = timeout_seconds
        self._cursor_signing_secret = cursor_signing_secret.encode("utf-8")
        self._clock = clock or _utc_now
        self._client_lock = threading.Lock()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            regions={Region.HK},
            datasets={Dataset.BARS},
            intervals={Interval.D1},
            max_page_size=10_000,
            supports_point_in_time=False,
            supports_revisions=False,
            supports_full_text=False,
            external_llm_allowed=False,
        )

    def assert_production_dataset_supported(self, dataset: Dataset) -> None:
        if dataset is not Dataset.BARS:
            raise UnsupportedCapabilityError("XtQuant is approved only for HK raw daily bars")

    @property
    def instrument_ids(self) -> tuple[str, ...]:
        return tuple(self._instruments_by_id)

    @property
    def request_timeout_seconds(self) -> float:
        return self._timeout_seconds

    def instrument_contracts(
        self,
        *,
        fetched_at: datetime,
        instrument_ids: Sequence[str] | None = None,
    ) -> list[Instrument]:
        instruments = (
            list(self._instruments_by_id.values())
            if instrument_ids is None
            else self._requested_instruments(instrument_ids)
        )
        return [
            _instrument_contract(instrument, fetched_at=fetched_at) for instrument in instruments
        ]

    async def healthcheck(self) -> ProviderHealth:
        checked_at = self._now()
        started_at = perf_counter()
        first_instrument = next(iter(self._instruments_by_id.values()))
        local_today = checked_at.astimezone(_HK_TIMEZONE).date()
        try:
            await self._query_rows(
                instruments=[first_instrument],
                start_date=local_today - timedelta(days=10),
                end_date=local_today,
                deadline_at=checked_at + timedelta(seconds=self._timeout_seconds),
            )
        except ProviderError as error:
            return ProviderHealth(
                provider_id=self.provider_id,
                status=(
                    "not_configured"
                    if isinstance(error, ProviderAuthenticationError)
                    or error.code == "XTQUANT_RUNTIME_MISSING"
                    else "down"
                ),
                checked_at=checked_at,
                latency_ms=int((perf_counter() - started_at) * 1000),
                message=error.code,
            )
        return ProviderHealth(
            provider_id=self.provider_id,
            status="ok",
            checked_at=checked_at,
            latency_ms=int((perf_counter() - started_at) * 1000),
        )

    async def aclose(self) -> None:
        """Release this worker's RPC client without stopping the shared data centre."""

        with self._client_lock:
            client = self._client
            self._client = None
            if client is not None:
                client.disconnect()

    async def fetch_bars(self, query: BarQuery, context: FetchContext) -> ProviderPage[MarketBar]:
        if query.interval is not Interval.D1 or query.adjustment is not Adjustment.RAW:
            raise UnsupportedCapabilityError("XtQuant supports raw daily bars only")
        fetched_at = self._now()
        if query.as_of < fetched_at:
            raise UnsupportedCapabilityError(
                "XtQuant daily bars do not provide historical point-in-time snapshots"
            )
        start_date = query.start.astimezone(_HK_TIMEZONE).date()
        end_date = (query.end - timedelta(microseconds=1)).astimezone(_HK_TIMEZONE).date()
        if end_date < start_date:
            return ProviderPage(items=[], fetched_at=fetched_at, complete=True)
        if end_date - start_date > _MAX_QUERY_WINDOW:
            raise UnsupportedCapabilityError("XtQuant query window exceeds five calendar years")

        fingerprint = self._cursor_fingerprint(query, context)
        offset, previous_id, expected_watermark = self._decode_cursor(query.cursor, fingerprint)
        instruments = self._requested_instruments(query.instrument_ids)
        rows_by_symbol, fetched_at = await self._query_rows(
            instruments=instruments,
            start_date=start_date,
            end_date=end_date,
            deadline_at=context.deadline_at,
        )
        all_items: list[MarketBar] = []
        warnings: list[WarningItem] = []
        for instrument in instruments:
            parsed, parsed_warnings = self._parse_rows(
                rows_by_symbol[instrument.source_symbol],
                instrument=instrument,
                query=query,
                fetched_at=fetched_at,
            )
            all_items.extend(parsed)
            warnings.extend(parsed_warnings)

        all_items.sort(key=lambda item: (item.bar_end, item.instrument_id, item.bar_id))
        _ensure_unique_bar_ids(all_items)
        watermark = canonical_json_checksum(
            [
                {
                    "provider_record_id": item.source.provider_record_id,
                    "checksum_sha256": item.source.checksum_sha256,
                }
                for item in all_items
            ]
        )
        if expected_watermark is not None and expected_watermark != watermark:
            raise ProviderCursorError(
                "XtQuant source changed while continuing a paginated query", code="SNAPSHOT_CHANGED"
            )
        if offset > len(all_items):
            raise ProviderCursorError(
                "XtQuant cursor is past the result set", code="INVALID_CURSOR"
            )
        if offset and (previous_id is None or all_items[offset - 1].bar_id != previous_id):
            raise ProviderCursorError(
                "XtQuant cursor predecessor does not match the source snapshot",
                code="SNAPSHOT_CHANGED",
            )
        items = all_items[offset : offset + query.limit]
        has_more = offset + len(items) < len(all_items)
        return ProviderPage(
            items=items,
            next_cursor=(
                self._encode_cursor(
                    fingerprint=fingerprint,
                    offset=offset + len(items),
                    last_bar_id=items[-1].bar_id,
                    source_watermark=watermark,
                )
                if has_more
                else None
            ),
            source_watermark=watermark,
            fetched_at=fetched_at,
            complete=not has_more,
            warnings=warnings,
        )

    async def _query_rows(
        self,
        *,
        instruments: Sequence[HkXtQuantInstrument],
        start_date: date,
        end_date: date,
        deadline_at: datetime,
    ) -> tuple[dict[str, list[dict[str, object]]], datetime]:
        remaining_seconds = (deadline_at.astimezone(UTC) - self._now()).total_seconds()
        if remaining_seconds <= 0:
            raise ProviderTimeoutError("XtQuant request deadline has elapsed", retryable=True)
        try:
            rows_by_symbol = await asyncio.wait_for(
                asyncio.to_thread(
                    self._query_rows_sync,
                    list(instruments),
                    start_date.strftime("%Y%m%d"),
                    end_date.strftime("%Y%m%d"),
                ),
                timeout=min(self._timeout_seconds, remaining_seconds),
            )
        except TimeoutError as error:
            raise ProviderTimeoutError("XtQuant request timed out", retryable=True) from error
        except ProviderError:
            raise
        except Exception as error:  # noqa: BLE001 - proprietary SDK boundary
            raise _classify_xtquant_error(error) from error
        return rows_by_symbol, self._now()

    def _query_rows_sync(
        self,
        instruments: list[HkXtQuantInstrument],
        start_time: str,
        end_time: str,
    ) -> dict[str, list[dict[str, object]]]:
        with self._client_lock:
            client = self._xtdata_client()
            client.connect(self._host, self._port)
            source_symbols = [instrument.source_symbol for instrument in instruments]
            client.download_history_data2(source_symbols, "1d", start_time, end_time)
            result = client.get_market_data_ex(
                field_list=[],
                stock_list=source_symbols,
                period="1d",
                start_time=start_time,
                end_time=end_time,
                count=-1,
                dividend_type="none",
                fill_data=False,
            )
            if not isinstance(result, Mapping):
                raise ProviderSchemaError("XtQuant daily-bar response is not a symbol mapping")
            rows_by_symbol: dict[str, list[dict[str, object]]] = {}
            for source_symbol in source_symbols:
                frame = result.get(source_symbol)
                rows = [] if frame is None else _frame_records(frame)
                if len(rows) > _MAX_ROWS_PER_INSTRUMENT:
                    raise ProviderSchemaError("XtQuant response exceeds the bounded row limit")
                rows_by_symbol[source_symbol] = rows
            return rows_by_symbol

    def _xtdata_client(self) -> XtQuantClient:
        if self._client is not None:
            return self._client
        try:
            from xtquant import xtdata
        except ImportError as error:
            raise ProviderUnavailableError(
                "XtQuant runtime is not installed; deploy the approved vendor package with the "
                "worker",
                code="XTQUANT_RUNTIME_MISSING",
            ) from error
        self._client = cast(XtQuantClient, xtdata)
        return self._client

    def _parse_rows(
        self,
        rows: Sequence[Mapping[str, object]],
        *,
        instrument: HkXtQuantInstrument,
        query: BarQuery,
        fetched_at: datetime,
    ) -> tuple[list[MarketBar], list[WarningItem]]:
        bars: list[MarketBar] = []
        warnings: list[WarningItem] = []
        previous_date: date | None = None
        for index, raw_row in enumerate(rows):
            try:
                trading_date = _trading_date(raw_row)
                if previous_date is not None and trading_date <= previous_date:
                    raise ProviderCursorError(
                        "XtQuant rows contain duplicate or descending trading dates",
                        code="INVALID_PAGINATION",
                    )
                previous_date = trading_date
                bar = _market_bar(
                    raw_row,
                    instrument=instrument,
                    trading_date=trading_date,
                    fetched_at=fetched_at,
                )
            except ProviderCursorError:
                raise
            except (ProviderSchemaError, ValueError) as error:
                warnings.append(
                    WarningItem(
                        code="PROVIDER_RECORD_QUARANTINED",
                        message=str(error)[:500],
                        scope=f"rows[{index}]",
                        details=_rejection_details(error, raw_row),
                    )
                )
                continue
            if query.start <= bar.bar_start < query.end:
                bars.append(bar)
        return bars, warnings

    def _requested_instruments(self, instrument_ids: Sequence[str]) -> list[HkXtQuantInstrument]:
        result: list[HkXtQuantInstrument] = []
        seen: set[str] = set()
        for instrument_id in instrument_ids:
            if instrument_id in seen:
                continue
            seen.add(instrument_id)
            try:
                result.append(self._instruments_by_id[instrument_id])
            except KeyError as error:
                raise UnsupportedCapabilityError(
                    f"XtQuant is not configured for instrument {instrument_id}"
                ) from error
        return result

    def _cursor_fingerprint(self, query: BarQuery, context: FetchContext) -> str:
        payload = query.model_dump(mode="json")
        payload.pop("cursor", None)
        payload["context_as_of"] = context.as_of.isoformat()
        return canonical_json_checksum(payload)

    def _encode_cursor(
        self,
        *,
        fingerprint: str,
        offset: int,
        last_bar_id: str,
        source_watermark: str,
    ) -> str:
        if offset < 0 or offset > _MAX_CURSOR_OFFSET:
            raise ValueError("XtQuant cursor offset is outside the allowed range")
        body = json.dumps(
            {
                "version": 1,
                "fingerprint": fingerprint,
                "offset": offset,
                "last_bar_id": last_bar_id,
                "source_watermark": source_watermark,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self._cursor_signing_secret, body, hashlib.sha256).digest()
        return ".".join(("xtquant-v1", _b64encode(body), _b64encode(signature)))

    def _decode_cursor(
        self, cursor: str | None, fingerprint: str
    ) -> tuple[int, str | None, str | None]:
        if cursor is None:
            return 0, None, None
        try:
            version, encoded_body, encoded_signature = cursor.split(".", maxsplit=2)
            body = base64.urlsafe_b64decode(_with_padding(encoded_body))
            signature = base64.urlsafe_b64decode(_with_padding(encoded_signature))
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as error:
            raise ProviderCursorError(
                "XtQuant cursor is malformed", code="INVALID_CURSOR"
            ) from error
        if not isinstance(payload, dict):
            raise ProviderCursorError("XtQuant cursor is not an object", code="INVALID_CURSOR")
        expected_signature = hmac.new(self._cursor_signing_secret, body, hashlib.sha256).digest()
        offset = payload.get("offset")
        last_bar_id = payload.get("last_bar_id")
        source_watermark = payload.get("source_watermark")
        if (
            version != "xtquant-v1"
            or not hmac.compare_digest(signature, expected_signature)
            or payload.get("version") != 1
            or payload.get("fingerprint") != fingerprint
            or not isinstance(offset, int)
            or offset < 0
            or offset > _MAX_CURSOR_OFFSET
            or not isinstance(last_bar_id, str)
            or not isinstance(source_watermark, str)
        ):
            raise ProviderCursorError("XtQuant cursor is not valid", code="INVALID_CURSOR")
        return offset, last_bar_id, source_watermark

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)


def register_hk_xtquant_provider_roles(
    registry: ProviderRegistry, provider: HkXtQuantDailyBarsProvider
) -> None:
    provider.assert_production_dataset_supported(Dataset.BARS)
    registry.register(provider)
    registry.bind_role(HK_XTQUANT_PRIMARY_ROLE, provider.provider_id, required_dataset=Dataset.BARS)
    registry.bind_role(HK_XTQUANT_EQUITY_ROLE, provider.provider_id, required_dataset=Dataset.BARS)


def _frame_records(frame: object) -> list[dict[str, object]]:
    try:
        reset = cast(Any, frame).reset_index()
        records = reset.to_dict(orient="records")
    except Exception as error:  # noqa: BLE001 - SDK returns pandas only at this boundary
        raise ProviderSchemaError(
            "XtQuant daily-bar frame cannot be converted to records"
        ) from error
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ProviderSchemaError("XtQuant daily-bar frame does not produce record objects")
    return [cast(dict[str, object], row) for row in records]


def _classify_xtquant_error(error: Exception) -> ProviderError:
    message = str(error).strip() or "XtQuant request failed"
    normalized = message.lower()
    if any(token in normalized for token in ("token", "auth", "login", "权限", "认证")):
        return ProviderAuthenticationError(message)
    if any(token in normalized for token in ("rate", "limit", "频繁", "too many")):
        return ProviderRateLimitError(message, retryable=True)
    if any(token in normalized for token in ("timeout", "超时")):
        return ProviderTimeoutError(message, retryable=True)
    return ProviderUnavailableError(message, retryable=True)


def _market_bar(
    raw_row: Mapping[str, object],
    *,
    instrument: HkXtQuantInstrument,
    trading_date: date,
    fetched_at: datetime,
) -> MarketBar:
    open_value = _decimal(raw_row.get("open"), path="open")
    high_value = _decimal(raw_row.get("high"), path="high")
    low_value = _decimal(raw_row.get("low"), path="low")
    close_value = _decimal(raw_row.get("close"), path="close")
    volume = _optional_decimal(raw_row.get("volume"), path="volume")
    turnover = _optional_decimal(raw_row.get("amount"), path="amount")
    session_open = datetime.combine(trading_date, time(9, 30), _HK_TIMEZONE).astimezone(UTC)
    session_close = datetime.combine(trading_date, time(16), _HK_TIMEZONE).astimezone(UTC)
    provider_record_id = (
        f"{HK_XTQUANT_PROVIDER_ID}:{instrument.source_symbol}:1d:{trading_date.isoformat()}:raw"
    )
    checksum_payload = {
        "date": trading_date.isoformat(),
        "open": str(open_value),
        "high": str(high_value),
        "low": str(low_value),
        "close": str(close_value),
        "preClose": _bounded_value(raw_row.get("preClose")),
        "volume": None if volume is None else str(volume),
        "amount": None if turnover is None else str(turnover),
    }
    quality_flags: list[str] = []
    if volume is None:
        quality_flags.append("VOLUME_MISSING")
    if turnover is None:
        quality_flags.append("TURNOVER_MISSING")
    return MarketBar(
        bar_id=stable_id(
            "bar_hk",
            instrument.instrument_id,
            Interval.D1.value,
            session_open,
            Adjustment.RAW.value,
            HK_XTQUANT_PROVIDER_ID,
        ),
        instrument_id=instrument.instrument_id,
        canonical_symbol=instrument.canonical_symbol,
        region=Region.HK,
        interval=Interval.D1,
        bar_start=session_open,
        bar_end=session_close,
        trading_date=trading_date,
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=volume,
        turnover=turnover,
        currency=instrument.currency,
        adjustment=Adjustment.RAW,
        available_at=fetched_at,
        availability_basis=AvailabilityBasis.FIRST_SEEN,
        source=SourceRef(
            provider_id=HK_XTQUANT_PROVIDER_ID,
            provider_record_id=provider_record_id,
            source_name="XtQuant",
            source_symbol=instrument.source_symbol,
            retrieved_at=fetched_at,
            checksum_sha256=canonical_json_checksum(checksum_payload),
        ),
        quality_flags=quality_flags,
    )


def _instrument_contract(instrument: HkXtQuantInstrument, *, fetched_at: datetime) -> Instrument:
    metadata = _INSTRUMENT_METADATA[instrument.source_symbol]
    checksum = canonical_json_checksum(
        {
            "canonical_symbol": instrument.canonical_symbol,
            "source_symbol": instrument.source_symbol,
            "name": metadata.name,
            "asset_class": metadata.asset_class.value,
            "listed_on": metadata.listed_on.isoformat(),
            "currency": instrument.currency,
        }
    )
    return Instrument(
        instrument_id=instrument.instrument_id,
        canonical_symbol=instrument.canonical_symbol,
        region=Region.HK,
        venue_mic="XHKG",
        local_symbol=instrument.source_symbol.removesuffix(".HK"),
        name=metadata.name,
        asset_class=metadata.asset_class,
        currency=instrument.currency,
        timezone="Asia/Hong_Kong",
        status=InstrumentStatus.ACTIVE,
        listed_on=metadata.listed_on,
        valid_from=metadata.listed_on,
        source=SourceRef(
            provider_id=HK_XTQUANT_PROVIDER_ID,
            provider_record_id=f"{HK_XTQUANT_PROVIDER_ID}:{instrument.source_symbol}:instrument",
            source_name="XtQuant",
            source_symbol=instrument.source_symbol,
            retrieved_at=fetched_at,
            checksum_sha256=checksum,
        ),
    )


def _trading_date(raw_row: Mapping[str, object]) -> date:
    raw_date = raw_row.get("index", raw_row.get("date"))
    parsed_date = _parse_date(raw_date) if raw_date is not None else None
    raw_time = raw_row.get("time")
    timestamp_date = _timestamp_date(raw_time) if raw_time is not None else None
    if parsed_date is None and timestamp_date is None:
        raise ProviderSchemaError("XtQuant daily-bar row has neither date nor time")
    if parsed_date is not None and timestamp_date is not None and parsed_date != timestamp_date:
        raise ProviderSchemaError("XtQuant daily-bar date and time disagree")
    if parsed_date is not None:
        return parsed_date
    assert timestamp_date is not None
    return timestamp_date


def _parse_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.astimezone(_HK_TIMEZONE).date() if value.tzinfo is not None else value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ProviderSchemaError("XtQuant daily-bar date must be a date string")
    candidate = value.strip()
    for format_string in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(candidate, format_string).date()
        except ValueError:
            continue
    raise ProviderSchemaError("XtQuant daily-bar date must be YYYYMMDD or ISO format")


def _timestamp_date(value: object) -> date:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderSchemaError("XtQuant daily-bar time must be epoch milliseconds")
    if not math.isfinite(float(value)) or value <= 0:
        raise ProviderSchemaError("XtQuant daily-bar time must be a positive finite timestamp")
    try:
        return datetime.fromtimestamp(float(value) / 1000, UTC).astimezone(_HK_TIMEZONE).date()
    except (OverflowError, OSError, ValueError) as error:
        raise ProviderSchemaError(
            "XtQuant daily-bar time is outside the supported range"
        ) from error


def _decimal(value: object, *, path: str) -> Decimal:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        raise ProviderSchemaError(f"XtQuant {path} must be a finite decimal")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise ProviderSchemaError(f"XtQuant {path} must be a decimal") from error
    if not parsed.is_finite():
        raise ProviderSchemaError(f"XtQuant {path} must be a finite decimal")
    return parsed


def _optional_decimal(value: object, *, path: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    parsed = _decimal(value, path=path)
    if parsed < 0:
        raise ProviderSchemaError(f"XtQuant {path} must not be negative")
    return parsed


def _rejection_details(error: Exception, raw_row: Mapping[str, object]) -> dict[str, object]:
    return {
        "rejection": {
            "error_code": error.code if isinstance(error, ProviderError) else "NORMALIZATION_ERROR",
            "redacted_payload": {
                "date": _bounded_value(raw_row.get("index", raw_row.get("date"))),
                "time": _bounded_value(raw_row.get("time")),
                "fields": sorted(str(key) for key in raw_row),
            },
        }
    }


def _bounded_value(value: object) -> str | int | float | None:
    if value is None or isinstance(value, (int, float)):
        return value
    return str(value)[:64]


def _ensure_unique_bar_ids(bars: Sequence[MarketBar]) -> None:
    seen: set[str] = set()
    for bar in bars:
        if bar.bar_id in seen:
            raise ProviderCursorError(
                "XtQuant response contains a duplicate bar", code="INVALID_PAGINATION"
            )
        seen.add(bar.bar_id)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _with_padding(value: str) -> bytes:
    return (value + "=" * (-len(value) % 4)).encode("ascii")


def _utc_now() -> datetime:
    return datetime.now(UTC)

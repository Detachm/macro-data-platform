"""Allowlisted BaoStock adapter for CN core-index daily bars.

BaoStock's public Python client exposes a synchronous, stateful session rather
than an HTTP API.  This adapter contains that boundary: calls run off the event
loop, each login/query/logout sequence is serialized, and only the reviewed
core-index mappings below can enter the platform.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import Final, Protocol, cast
from zoneinfo import ZoneInfo

import baostock as bs

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

BAOSTOCK_PROVIDER_ID: Final = "cn.baostock.v1"
BAOSTOCK_PRIMARY_ROLE: Final = "cn.bars.primary"
BAOSTOCK_SOURCE_URL: Final = "https://www.baostock.com/"
_MAX_CURSOR_OFFSET: Final = 100_000
_MAX_QUERY_WINDOW: Final = timedelta(days=5 * 366)
_MAX_ROWS_PER_INSTRUMENT: Final = 2_500
_CN_TIMEZONE: Final = ZoneInfo("Asia/Shanghai")
_DAILY_FIELDS: Final = "date,code,open,high,low,close,preclose,volume,amount,pctChg"
_EXPECTED_FIELDS: Final = tuple(_DAILY_FIELDS.split(","))


class _BaoStockResult(Protocol):
    error_code: str
    error_msg: str
    fields: list[str]

    def next(self) -> bool: ...

    def get_row_data(self) -> list[str]: ...


class BaoStockClient(Protocol):
    def login(self) -> _BaoStockResult: ...

    def logout(self) -> _BaoStockResult: ...

    def query_history_k_data_plus(
        self,
        code: str,
        fields: str,
        *,
        start_date: str,
        end_date: str,
        frequency: str,
        adjustflag: str,
    ) -> _BaoStockResult: ...


@dataclass(frozen=True, slots=True)
class BaoStockInstrument:
    """Reviewed platform identity for one BaoStock index symbol."""

    instrument_id: str
    canonical_symbol: str
    source_symbol: str
    currency: str = "CNY"

    def __post_init__(self) -> None:
        metadata = _INSTRUMENT_METADATA.get(self.source_symbol)
        if not self.instrument_id.strip():
            raise ValueError("BaoStock instrument_id must not be empty")
        if metadata is None:
            raise ValueError("BaoStock symbol is not in the approved core-index allowlist")
        if self.canonical_symbol != metadata.canonical_symbol:
            raise ValueError("BaoStock canonical_symbol does not match the approved mapping")
        if self.currency != "CNY":
            raise ValueError("BaoStock core-index daily bars are limited to CNY")


@dataclass(frozen=True, slots=True)
class _BaoStockInstrumentMetadata:
    canonical_symbol: str
    venue_mic: str
    local_symbol: str
    name: str
    listed_on: date


_INSTRUMENT_METADATA: Final = {
    "sh.000001": _BaoStockInstrumentMetadata(
        canonical_symbol="XSHG:000001",
        venue_mic="XSHG",
        local_symbol="000001",
        name="SSE Composite Index",
        listed_on=date(1990, 12, 19),
    ),
    "sh.000300": _BaoStockInstrumentMetadata(
        canonical_symbol="XSHG:000300",
        venue_mic="XSHG",
        local_symbol="000300",
        name="CSI 300 Index",
        listed_on=date(2005, 4, 8),
    ),
    "sz.399001": _BaoStockInstrumentMetadata(
        canonical_symbol="XSHE:399001",
        venue_mic="XSHE",
        local_symbol="399001",
        name="Shenzhen Component Index",
        listed_on=date(1991, 4, 3),
    ),
}

BAOSTOCK_DEFAULT_INSTRUMENTS: Final = (
    BaoStockInstrument(
        instrument_id="ins_cn_index_sse_composite",
        canonical_symbol="XSHG:000001",
        source_symbol="sh.000001",
    ),
    BaoStockInstrument(
        instrument_id="ins_cn_index_csi300",
        canonical_symbol="XSHG:000300",
        source_symbol="sh.000300",
    ),
    BaoStockInstrument(
        instrument_id="ins_cn_index_szse_component",
        canonical_symbol="XSHE:399001",
        source_symbol="sz.399001",
    ),
)


class BaoStockDailyBarsProvider:
    """Fetch raw, daily CN core-index bars from the public BaoStock client."""

    provider_id: Final[str] = BAOSTOCK_PROVIDER_ID
    source_name: Final[str] = "BaoStock"

    def __init__(
        self,
        *,
        instruments: Sequence[BaoStockInstrument] = BAOSTOCK_DEFAULT_INSTRUMENTS,
        client: BaoStockClient | None = None,
        timeout_seconds: float = 30,
        cursor_signing_secret: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("BaoStock timeout_seconds must be positive")
        if not cursor_signing_secret:
            raise ValueError("BaoStock cursor_signing_secret must not be empty")
        if not instruments:
            raise ValueError("BaoStock requires at least one approved instrument")
        by_id = {instrument.instrument_id: instrument for instrument in instruments}
        by_symbol = {instrument.source_symbol: instrument for instrument in instruments}
        if len(by_id) != len(instruments) or len(by_symbol) != len(instruments):
            raise ValueError("BaoStock instruments and source symbols must be unique")
        self._instruments_by_id = by_id
        self._client = client if client is not None else cast(BaoStockClient, bs)
        self._timeout_seconds = timeout_seconds
        self._cursor_signing_secret = cursor_signing_secret.encode("utf-8")
        self._clock = clock or _utc_now
        self._client_lock = threading.Lock()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            regions={Region.CN},
            datasets={Dataset.BARS},
            intervals={Interval.D1},
            max_page_size=_MAX_ROWS_PER_INSTRUMENT * len(self._instruments_by_id),
            supports_point_in_time=False,
            supports_revisions=False,
            supports_full_text=False,
            external_llm_allowed=False,
        )

    def assert_production_dataset_supported(self, dataset: Dataset) -> None:
        if dataset is not Dataset.BARS:
            raise UnsupportedCapabilityError(
                "BaoStock is approved only for CN raw daily index bars"
            )

    @property
    def instrument_ids(self) -> tuple[str, ...]:
        return tuple(self._instruments_by_id)

    @property
    def request_timeout_seconds(self) -> float:
        return self._timeout_seconds

    def instrument_contracts(self, *, fetched_at: datetime) -> list[Instrument]:
        return [
            _instrument_contract(instrument, fetched_at=fetched_at)
            for instrument in self._instruments_by_id.values()
        ]

    async def healthcheck(self) -> ProviderHealth:
        checked_at = self._now()
        started_at = perf_counter()
        first_instrument = next(iter(self._instruments_by_id.values()))
        local_today = checked_at.astimezone(_CN_TIMEZONE).date()
        try:
            await self._query_rows(
                instrument=first_instrument,
                start_date=local_today - timedelta(days=10),
                end_date=local_today,
                deadline_at=checked_at + timedelta(seconds=self._timeout_seconds),
            )
        except ProviderError as error:
            return ProviderHealth(
                provider_id=self.provider_id,
                status=(
                    "not_configured" if isinstance(error, ProviderAuthenticationError) else "down"
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
        """The client is logged out after every request, so there is no open resource."""

    async def fetch_bars(self, query: BarQuery, context: FetchContext) -> ProviderPage[MarketBar]:
        if query.interval is not Interval.D1 or query.adjustment is not Adjustment.RAW:
            raise UnsupportedCapabilityError("BaoStock supports raw daily bars only")
        fetched_at = self._now()
        if query.as_of < fetched_at:
            raise UnsupportedCapabilityError(
                "BaoStock daily bars do not provide historical point-in-time snapshots"
            )
        start_date = query.start.astimezone(_CN_TIMEZONE).date()
        end_date = (query.end - timedelta(microseconds=1)).astimezone(_CN_TIMEZONE).date()
        if end_date < start_date:
            return ProviderPage(items=[], fetched_at=fetched_at, complete=True)
        if end_date - start_date > _MAX_QUERY_WINDOW:
            raise UnsupportedCapabilityError("BaoStock query window exceeds five calendar years")

        fingerprint = self._cursor_fingerprint(query, context)
        offset, previous_id, expected_watermark = self._decode_cursor(query.cursor, fingerprint)
        all_items: list[MarketBar] = []
        warnings: list[WarningItem] = []
        for instrument in self._requested_instruments(query.instrument_ids):
            rows, requested_at = await self._query_rows(
                instrument=instrument,
                start_date=start_date,
                end_date=end_date,
                deadline_at=context.deadline_at,
            )
            fetched_at = max(fetched_at, requested_at)
            parsed, parsed_warnings = self._parse_rows(
                rows,
                instrument=instrument,
                query=query,
                fetched_at=requested_at,
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
                "BaoStock source changed while continuing a paginated query",
                code="SNAPSHOT_CHANGED",
            )
        if offset > len(all_items):
            raise ProviderCursorError(
                "BaoStock cursor is past the result set", code="INVALID_CURSOR"
            )
        if offset and (previous_id is None or all_items[offset - 1].bar_id != previous_id):
            raise ProviderCursorError(
                "BaoStock cursor predecessor does not match the source snapshot",
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
        instrument: BaoStockInstrument,
        start_date: date,
        end_date: date,
        deadline_at: datetime,
    ) -> tuple[list[dict[str, str]], datetime]:
        remaining_seconds = (deadline_at.astimezone(UTC) - self._now()).total_seconds()
        if remaining_seconds <= 0:
            raise ProviderTimeoutError("BaoStock request deadline has elapsed", retryable=True)
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(
                    self._query_rows_sync,
                    instrument.source_symbol,
                    start_date.isoformat(),
                    end_date.isoformat(),
                ),
                timeout=min(self._timeout_seconds, remaining_seconds),
            )
        except TimeoutError as error:
            raise ProviderTimeoutError("BaoStock request timed out", retryable=True) from error
        except ProviderError:
            raise
        except Exception as error:  # noqa: BLE001 - third-party socket boundary
            raise ProviderUnavailableError("BaoStock request failed", retryable=True) from error
        return rows, self._now()

    def _query_rows_sync(
        self, source_symbol: str, start_date: str, end_date: str
    ) -> list[dict[str, str]]:
        with self._client_lock:
            login = self._client.login()
            _raise_for_result(login, operation="login")
            try:
                result = self._client.query_history_k_data_plus(
                    source_symbol,
                    _DAILY_FIELDS,
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag="3",
                )
                _raise_for_result(result, operation="daily-bar query")
                if tuple(result.fields) != _EXPECTED_FIELDS:
                    raise ProviderSchemaError(
                        "BaoStock daily-bar fields do not match the approved schema"
                    )
                rows: list[dict[str, str]] = []
                while result.next():
                    values = result.get_row_data()
                    if len(values) != len(_EXPECTED_FIELDS):
                        raise ProviderSchemaError(
                            "BaoStock daily-bar row has an unexpected field count"
                        )
                    rows.append(dict(zip(_EXPECTED_FIELDS, values, strict=True)))
                    if len(rows) > _MAX_ROWS_PER_INSTRUMENT:
                        raise ProviderSchemaError("BaoStock response exceeds the bounded row limit")
                return rows
            finally:
                self._client.logout()

    def _parse_rows(
        self,
        rows: Sequence[Mapping[str, str]],
        *,
        instrument: BaoStockInstrument,
        query: BarQuery,
        fetched_at: datetime,
    ) -> tuple[list[MarketBar], list[WarningItem]]:
        bars: list[MarketBar] = []
        warnings: list[WarningItem] = []
        previous_date: date | None = None
        for index, raw_row in enumerate(rows):
            try:
                trading_date = _parse_date(raw_row.get("date"), path="date")
                if previous_date is not None and trading_date <= previous_date:
                    raise ProviderCursorError(
                        "BaoStock rows contain duplicate or descending trading dates",
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
                        details={
                            "rejection": {
                                "error_code": getattr(error, "code", "SCHEMA_DRIFT"),
                                "redacted_payload": dict(raw_row),
                            }
                        },
                    )
                )
                continue
            if query.start <= bar.bar_start < query.end:
                bars.append(bar)
        return bars, warnings

    def _requested_instruments(self, instrument_ids: Sequence[str]) -> list[BaoStockInstrument]:
        result: list[BaoStockInstrument] = []
        seen: set[str] = set()
        for instrument_id in instrument_ids:
            if instrument_id in seen:
                continue
            seen.add(instrument_id)
            try:
                result.append(self._instruments_by_id[instrument_id])
            except KeyError as error:
                raise UnsupportedCapabilityError(
                    f"BaoStock is not configured for instrument {instrument_id}"
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
            raise ValueError("BaoStock cursor offset is outside the allowed range")
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
        return ".".join(("baostock-v1", _b64encode(body), _b64encode(signature)))

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
                "BaoStock cursor is malformed", code="INVALID_CURSOR"
            ) from error
        if not isinstance(payload, dict):
            raise ProviderCursorError("BaoStock cursor is not an object", code="INVALID_CURSOR")
        expected_signature = hmac.new(self._cursor_signing_secret, body, hashlib.sha256).digest()
        offset = payload.get("offset")
        last_bar_id = payload.get("last_bar_id")
        source_watermark = payload.get("source_watermark")
        if (
            version != "baostock-v1"
            or not hmac.compare_digest(signature, expected_signature)
            or payload.get("version") != 1
            or payload.get("fingerprint") != fingerprint
            or not isinstance(offset, int)
            or offset < 0
            or offset > _MAX_CURSOR_OFFSET
            or not isinstance(last_bar_id, str)
            or not isinstance(source_watermark, str)
        ):
            raise ProviderCursorError("BaoStock cursor is not valid", code="INVALID_CURSOR")
        return offset, last_bar_id, source_watermark

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)


def register_cn_baostock_provider_roles(
    registry: ProviderRegistry, provider: BaoStockDailyBarsProvider
) -> None:
    provider.assert_production_dataset_supported(Dataset.BARS)
    registry.register(provider)
    registry.bind_role(BAOSTOCK_PRIMARY_ROLE, provider.provider_id, required_dataset=Dataset.BARS)


def _raise_for_result(result: _BaoStockResult, *, operation: str) -> None:
    if result.error_code == "0":
        return
    message = result.error_msg.strip() or f"BaoStock {operation} failed"
    lower_message = message.lower()
    if any(token in lower_message for token in ("login", "登录", "auth", "permission", "权限")):
        raise ProviderAuthenticationError(message)
    if any(token in lower_message for token in ("频繁", "rate", "limit", "too many")):
        raise ProviderRateLimitError(message, retryable=True)
    if any(token in lower_message for token in ("timeout", "超时")):
        raise ProviderTimeoutError(message, retryable=True)
    raise ProviderUnavailableError(message, retryable=True)


def _market_bar(
    raw_row: Mapping[str, str],
    *,
    instrument: BaoStockInstrument,
    trading_date: date,
    fetched_at: datetime,
) -> MarketBar:
    source_symbol = _required_text(raw_row.get("code"), path="code")
    if source_symbol != instrument.source_symbol:
        raise ProviderSchemaError("BaoStock response symbol does not match the request")
    open_value = _decimal(raw_row.get("open"), path="open")
    high_value = _decimal(raw_row.get("high"), path="high")
    low_value = _decimal(raw_row.get("low"), path="low")
    close_value = _decimal(raw_row.get("close"), path="close")
    preclose_value = _decimal(raw_row.get("preclose"), path="preclose")
    volume = _optional_decimal(raw_row.get("volume"), path="volume")
    turnover = _optional_decimal(raw_row.get("amount"), path="amount")
    session_open = datetime.combine(trading_date, time(9, 30), _CN_TIMEZONE).astimezone(UTC)
    session_close = datetime.combine(trading_date, time(15), _CN_TIMEZONE).astimezone(UTC)
    provider_record_id = f"{BAOSTOCK_PROVIDER_ID}:{source_symbol}:1d:{trading_date.isoformat()}:raw"
    checksum_payload = {
        "date": trading_date.isoformat(),
        "code": source_symbol,
        "open": str(open_value),
        "high": str(high_value),
        "low": str(low_value),
        "close": str(close_value),
        "preclose": str(preclose_value),
        "volume": None if volume is None else str(volume),
        "amount": None if turnover is None else str(turnover),
        "pctChg": raw_row.get("pctChg"),
    }
    quality_flags: list[str] = []
    if volume is None:
        quality_flags.append("VOLUME_MISSING")
    if turnover is None:
        quality_flags.append("TURNOVER_MISSING")
    return MarketBar(
        bar_id=stable_id(
            "bar_cn",
            instrument.instrument_id,
            Interval.D1.value,
            session_open,
            Adjustment.RAW.value,
            BAOSTOCK_PROVIDER_ID,
        ),
        instrument_id=instrument.instrument_id,
        canonical_symbol=instrument.canonical_symbol,
        region=Region.CN,
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
            provider_id=BAOSTOCK_PROVIDER_ID,
            provider_record_id=provider_record_id,
            source_name="BaoStock",
            source_url=BAOSTOCK_SOURCE_URL,
            source_symbol=source_symbol,
            retrieved_at=fetched_at,
            checksum_sha256=canonical_json_checksum(checksum_payload),
        ),
        quality_flags=quality_flags,
    )


def _instrument_contract(instrument: BaoStockInstrument, *, fetched_at: datetime) -> Instrument:
    metadata = _INSTRUMENT_METADATA[instrument.source_symbol]
    checksum = canonical_json_checksum(
        {
            "canonical_symbol": instrument.canonical_symbol,
            "source_symbol": instrument.source_symbol,
            "venue_mic": metadata.venue_mic,
            "name": metadata.name,
            "listed_on": metadata.listed_on.isoformat(),
            "currency": instrument.currency,
        }
    )
    return Instrument(
        instrument_id=instrument.instrument_id,
        canonical_symbol=instrument.canonical_symbol,
        region=Region.CN,
        venue_mic=metadata.venue_mic,
        local_symbol=metadata.local_symbol,
        name=metadata.name,
        asset_class=AssetClass.INDEX,
        currency=instrument.currency,
        timezone="Asia/Shanghai",
        status=InstrumentStatus.ACTIVE,
        listed_on=metadata.listed_on,
        valid_from=metadata.listed_on,
        source=SourceRef(
            provider_id=BAOSTOCK_PROVIDER_ID,
            provider_record_id=f"{BAOSTOCK_PROVIDER_ID}:{instrument.source_symbol}:instrument",
            source_name="BaoStock",
            source_url=BAOSTOCK_SOURCE_URL,
            source_symbol=instrument.source_symbol,
            retrieved_at=fetched_at,
            checksum_sha256=checksum,
        ),
    )


def _parse_date(value: object, *, path: str) -> date:
    if not isinstance(value, str):
        raise ProviderSchemaError(f"BaoStock {path} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ProviderSchemaError(f"BaoStock {path} must be an ISO date") from error


def _required_text(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderSchemaError(f"BaoStock {path} must be a non-empty string")
    return value.strip()


def _decimal(value: object, *, path: str) -> Decimal:
    text_value = _required_text(value, path=path)
    try:
        parsed = Decimal(text_value)
    except (InvalidOperation, ValueError) as error:
        raise ProviderSchemaError(f"BaoStock {path} must be a decimal") from error
    if not parsed.is_finite():
        raise ProviderSchemaError(f"BaoStock {path} must be finite")
    return parsed


def _optional_decimal(value: object, *, path: str) -> Decimal | None:
    if value is None or value == "":
        return None
    parsed = _decimal(value, path=path)
    if parsed < 0:
        raise ProviderSchemaError(f"BaoStock {path} must not be negative")
    return parsed


def _ensure_unique_bar_ids(bars: Sequence[MarketBar]) -> None:
    seen: set[str] = set()
    for bar in bars:
        if bar.bar_id in seen:
            raise ProviderCursorError(
                "BaoStock response contains a duplicate bar", code="INVALID_PAGINATION"
            )
        seen.add(bar.bar_id)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _with_padding(value: str) -> bytes:
    return (value + "=" * (-len(value) % 4)).encode("ascii")


def _utc_now() -> datetime:
    return datetime.now(UTC)

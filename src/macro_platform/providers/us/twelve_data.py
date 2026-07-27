"""Allowlisted Twelve Data adapter for internally stored US daily ETF bars.

This module intentionally keeps the provider-specific HTTP classification,
opaque cursor, and OHLCV mapping together.  They share one credential-bearing
request boundary and one snapshot checksum; splitting either half before the
generic live transport from #28 is available would duplicate that boundary in
another US-only module.  The public seams remain the provider constructor and
``fetch_bars``; a future shared transport can be substituted without changing
those contracts.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from time import perf_counter
from typing import Any, Final
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr

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
from macro_platform.normalization.common import (
    canonical_json_checksum,
    normalize_canonical_symbol,
    stable_id,
)
from macro_platform.normalization.us import us_equity_session_window
from macro_platform.normalization.us.errors import UsNormalizationError
from macro_platform.observability.metrics import (
    PROVIDER_DURATION,
    PROVIDER_LAST_SUCCESS,
    PROVIDER_REQUESTS,
)
from macro_platform.providers.base import (
    ProviderAuthenticationError,
    ProviderAuthorizationError,
    ProviderCursorError,
    ProviderError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
)
from macro_platform.providers.registry import ProviderRegistry

TWELVE_DATA_PROVIDER_ID: Final = "us.twelve-data.v1"
TWELVE_DATA_BASE_URL: Final = "https://api.twelvedata.com/time_series"
TWELVE_DATA_ALLOWED_HOST: Final = "api.twelvedata.com"
TWELVE_DATA_PRIMARY_ROLE: Final = "us.market.primary"
TWELVE_DATA_ALLOWED_SYMBOLS: Final = frozenset({"SPY", "QQQ", "DIA"})
_MAX_OUTPUT_SIZE: Final = 5000
_MAX_CURSOR_OFFSET: Final = 100_000
_MAX_QUERY_WINDOW: Final = timedelta(days=5 * 366)
_MAX_REQUEST_ATTEMPTS: Final = 3
_MAX_RETRY_DELAY_SECONDS: Final = 30
_PIT_CLOCK_SKEW: Final = timedelta(seconds=5)
_NEW_YORK: Final = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class TwelveDataInstrument:
    """The pre-approved platform identity for one provider symbol.

    Twelve Data only supplies a ticker in the time-series response.  The
    adapter therefore receives the reviewed instrument mapping rather than
    attempting to infer an instrument identity from a provider response.
    """

    instrument_id: str
    canonical_symbol: str
    source_symbol: str
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("Twelve Data instrument_id must not be empty")
        if self.canonical_symbol != normalize_canonical_symbol(self.canonical_symbol):
            raise ValueError("Twelve Data canonical_symbol must already be normalized")
        if not self.source_symbol or self.source_symbol != self.source_symbol.upper():
            raise ValueError("Twelve Data source_symbol must be non-empty uppercase")
        if len(self.source_symbol) > 32 or not self.source_symbol.isascii():
            raise ValueError("Twelve Data source_symbol is invalid")
        if self.currency != "USD":
            raise ValueError("Twelve Data daily ETF bars are limited to USD")
        if self.source_symbol not in TWELVE_DATA_ALLOWED_SYMBOLS:
            raise ValueError("Twelve Data symbol is not in the approved ETF allowlist")


@dataclass(frozen=True, slots=True)
class _TwelveDataInstrumentMetadata:
    venue_mic: str
    name: str
    valid_from: date


_INSTRUMENT_METADATA: Final = {
    "SPY": _TwelveDataInstrumentMetadata(
        venue_mic="ARCX",
        name="SPDR S&P 500 ETF Trust",
        valid_from=date(1993, 1, 22),
    ),
    "QQQ": _TwelveDataInstrumentMetadata(
        venue_mic="XNAS",
        name="Invesco QQQ Trust, Series 1",
        valid_from=date(1999, 3, 10),
    ),
    "DIA": _TwelveDataInstrumentMetadata(
        venue_mic="ARCX",
        name="SPDR Dow Jones Industrial Average ETF Trust",
        valid_from=date(1998, 1, 14),
    ),
}


TWELVE_DATA_DEFAULT_INSTRUMENTS: Final = (
    TwelveDataInstrument(
        instrument_id="ins_us_etf_spy",
        canonical_symbol="ARCX:SPY",
        source_symbol="SPY",
    ),
    TwelveDataInstrument(
        instrument_id="ins_us_etf_qqq",
        canonical_symbol="XNAS:QQQ",
        source_symbol="QQQ",
    ),
    TwelveDataInstrument(
        instrument_id="ins_us_etf_dia",
        canonical_symbol="ARCX:DIA",
        source_symbol="DIA",
    ),
)


class TwelveDataDailyBarsProvider:
    """Live, bounded provider for the approved US daily ETF bar allowlist."""

    provider_id: Final[str] = TWELVE_DATA_PROVIDER_ID
    source_name: Final[str] = "Twelve Data"

    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        instruments: Sequence[TwelveDataInstrument],
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30,
        cursor_signing_secret: str,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Twelve Data timeout_seconds must be positive")
        if not cursor_signing_secret:
            raise ValueError("Twelve Data cursor_signing_secret must not be empty")
        if not instruments:
            raise ValueError("Twelve Data requires at least one approved instrument")

        by_instrument_id = {instrument.instrument_id: instrument for instrument in instruments}
        by_source_symbol = {instrument.source_symbol: instrument for instrument in instruments}
        if len(by_instrument_id) != len(instruments):
            raise ValueError("Twelve Data instrument_ids must be unique")
        if len(by_source_symbol) != len(instruments):
            raise ValueError("Twelve Data source_symbols must be unique")

        self._api_key = api_key
        self._instruments_by_id = by_instrument_id
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._timeout_seconds = timeout_seconds
        self._cursor_signing_secret = cursor_signing_secret.encode("utf-8")
        self._clock = clock or _utc_now
        self._sleeper = sleeper

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            regions={Region.US},
            datasets={Dataset.BARS},
            intervals={Interval.D1},
            max_page_size=_MAX_OUTPUT_SIZE,
            supports_point_in_time=False,
            supports_revisions=False,
            supports_full_text=False,
            external_llm_allowed=False,
        )

    def assert_production_dataset_supported(self, dataset: Dataset) -> None:
        if dataset is not Dataset.BARS:
            raise UnsupportedCapabilityError(
                "Twelve Data Basic is approved only for raw US daily bars"
            )

    @property
    def instrument_ids(self) -> tuple[str, ...]:
        """Stable, reviewed instrument IDs available to this adapter instance."""

        return tuple(self._instruments_by_id)

    @property
    def request_timeout_seconds(self) -> float:
        """Bound used for one upstream request, independent of business dates."""

        return self._timeout_seconds

    def instrument_contracts(self, *, fetched_at: datetime) -> list[Instrument]:
        """Return the reviewed, effective-dated ETF mappings required by bar FKs."""

        return [
            _instrument_contract(instrument, fetched_at=fetched_at)
            for instrument in self._instruments_by_id.values()
        ]

    async def healthcheck(self) -> ProviderHealth:
        checked_at = self._now()
        if not self._has_api_key:
            return ProviderHealth(
                provider_id=self.provider_id,
                status="not_configured",
                checked_at=checked_at,
                latency_ms=0,
                message="Twelve Data API key is not configured",
            )

        started_at = perf_counter()
        first_instrument = next(iter(self._instruments_by_id.values()))
        try:
            await self._fetch_payload(
                symbol=first_instrument.source_symbol,
                start_date=checked_at.date(),
                end_date=checked_at.date(),
                outputsize=1,
                deadline_at=checked_at + timedelta(seconds=self._timeout_seconds),
            )
        except Exception as error:  # noqa: BLE001 - health must never stop the application
            return ProviderHealth(
                provider_id=self.provider_id,
                status=(
                    "not_configured"
                    if isinstance(error, (ProviderAuthenticationError, ProviderAuthorizationError))
                    else "down"
                ),
                checked_at=checked_at,
                latency_ms=int((perf_counter() - started_at) * 1000),
                message=type(error).__name__,
            )
        return ProviderHealth(
            provider_id=self.provider_id,
            status="ok",
            checked_at=checked_at,
            latency_ms=int((perf_counter() - started_at) * 1000),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_bars(self, query: BarQuery, context: FetchContext) -> ProviderPage[MarketBar]:
        started_at = perf_counter()
        try:
            page = await self._fetch_bars(query, context)
        except ProviderError as error:
            PROVIDER_REQUESTS.labels(
                provider_role=TWELVE_DATA_PRIMARY_ROLE,
                dataset=Dataset.BARS.value,
                status=error.code,
            ).inc()
            raise
        PROVIDER_REQUESTS.labels(
            provider_role=TWELVE_DATA_PRIMARY_ROLE,
            dataset=Dataset.BARS.value,
            status="success",
        ).inc()
        PROVIDER_DURATION.labels(
            provider_role=TWELVE_DATA_PRIMARY_ROLE,
            dataset=Dataset.BARS.value,
        ).observe(perf_counter() - started_at)
        PROVIDER_LAST_SUCCESS.labels(
            provider_role=TWELVE_DATA_PRIMARY_ROLE,
            dataset=Dataset.BARS.value,
            region=Region.US.value,
        ).set(page.fetched_at.timestamp())
        return page

    async def _fetch_bars(self, query: BarQuery, context: FetchContext) -> ProviderPage[MarketBar]:
        if query.interval is not Interval.D1 or query.adjustment is not Adjustment.RAW:
            raise UnsupportedCapabilityError("Twelve Data supports raw daily bars only")
        if not self._has_api_key:
            raise ProviderAuthenticationError("Twelve Data API key is not configured")
        fetched_at = self._now()
        if query.as_of < fetched_at:
            raise UnsupportedCapabilityError(
                "Twelve Data daily bars do not provide historical point-in-time snapshots"
            )

        start_date = query.start.astimezone(_NEW_YORK).date()
        end_date = (query.end - timedelta(microseconds=1)).astimezone(_NEW_YORK).date()
        if end_date < start_date:
            return ProviderPage(
                items=[],
                fetched_at=fetched_at,
                complete=True,
            )
        if end_date - start_date > _MAX_QUERY_WINDOW:
            raise UnsupportedCapabilityError("Twelve Data query window exceeds five calendar years")

        fingerprint = self._cursor_fingerprint(query, context)
        offset, previous_id, expected_watermark = self._decode_cursor(query.cursor, fingerprint)
        requested_instruments = self._requested_instruments(query.instrument_ids)
        all_items: list[MarketBar] = []
        warnings: list[WarningItem] = []
        for instrument in requested_instruments:
            payload, request_fetched_at = await self._fetch_payload(
                symbol=instrument.source_symbol,
                start_date=start_date,
                end_date=end_date,
                outputsize=_MAX_OUTPUT_SIZE,
                deadline_at=context.deadline_at,
            )
            fetched_at = max(fetched_at, request_fetched_at)
            parsed, parsed_warnings = self._parse_payload(
                payload,
                instrument=instrument,
                query=query,
                fetched_at=request_fetched_at,
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
                "Twelve Data source changed while continuing a paginated query",
                code="SNAPSHOT_CHANGED",
            )
        if offset > len(all_items):
            raise ProviderCursorError(
                "Twelve Data cursor is past the result set", code="INVALID_CURSOR"
            )
        if offset and (previous_id is None or all_items[offset - 1].bar_id != previous_id):
            raise ProviderCursorError(
                "Twelve Data cursor predecessor does not match the source snapshot",
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

    @property
    def _has_api_key(self) -> bool:
        return self._api_key is not None and bool(self._api_key.get_secret_value().strip())

    def _requested_instruments(self, instrument_ids: Sequence[str]) -> list[TwelveDataInstrument]:
        result: list[TwelveDataInstrument] = []
        seen: set[str] = set()
        for instrument_id in instrument_ids:
            if instrument_id in seen:
                continue
            seen.add(instrument_id)
            try:
                result.append(self._instruments_by_id[instrument_id])
            except KeyError as error:
                raise UnsupportedCapabilityError(
                    f"Twelve Data is not configured for instrument {instrument_id}"
                ) from error
        return result

    async def _fetch_payload(
        self,
        *,
        symbol: str,
        start_date: date,
        end_date: date,
        outputsize: int,
        deadline_at: datetime,
    ) -> tuple[dict[str, Any], datetime]:
        for attempt in range(_MAX_REQUEST_ATTEMPTS):
            now = self._now()
            remaining_seconds = (deadline_at.astimezone(UTC) - now).total_seconds()
            if remaining_seconds <= 0:
                raise ProviderTimeoutError(
                    "Twelve Data request deadline has elapsed", retryable=True
                )
            try:
                try:
                    response = await self._client.get(
                        TWELVE_DATA_BASE_URL,
                        params={
                            "symbol": symbol,
                            "interval": "1day",
                            "start_date": start_date.isoformat(),
                            "end_date": end_date.isoformat(),
                            "outputsize": outputsize,
                            "order": "ASC",
                        },
                        headers={
                            "Authorization": (
                                f"apikey {self._api_key.get_secret_value()}"
                                if self._api_key
                                else ""
                            )
                        },
                        timeout=min(self._timeout_seconds, remaining_seconds),
                    )
                except httpx.TimeoutException as error:
                    raise ProviderTimeoutError(
                        "Twelve Data request timed out", retryable=True
                    ) from error
                except httpx.HTTPError as error:
                    raise ProviderUnavailableError(
                        "Twelve Data request failed", retryable=True
                    ) from error

                self._raise_for_status(response, now=now)
                payload = self._json_payload(response)
                self._raise_for_payload_error(payload, now=now)
                return payload, self._now()
            except (
                ProviderRateLimitError,
                ProviderTimeoutError,
                ProviderUnavailableError,
            ) as error:
                if not error.retryable or attempt == _MAX_REQUEST_ATTEMPTS - 1:
                    raise
                retry_delay = _retry_delay_seconds(error, attempt=attempt)
                if retry_delay >= remaining_seconds:
                    raise
                await self._sleeper(retry_delay)

        raise RuntimeError("Twelve Data retry loop exited without a provider result")

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, now: datetime) -> None:
        if response.status_code in {401, 407}:
            raise ProviderAuthenticationError("Twelve Data authentication failed")
        if response.status_code == 403:
            raise ProviderAuthorizationError("Twelve Data denied the request")
        if response.status_code == 429:
            raise ProviderRateLimitError(
                "Twelve Data rate limited the request",
                retryable=True,
                retry_after_seconds=_retry_after_seconds(response, now=now),
            )
        if 500 <= response.status_code <= 599:
            raise ProviderUnavailableError("Twelve Data is unavailable", retryable=True)
        if response.status_code >= 400:
            raise ProviderError(f"Twelve Data returned HTTP {response.status_code}")

    @staticmethod
    def _json_payload(response: httpx.Response) -> dict[str, Any]:
        content_type = response.headers.get("Content-Type", "").lower()
        body_prefix = response.text.lstrip().lower()[:256]
        if "text/html" in content_type or body_prefix.startswith(("<!doctype html", "<html")):
            raise ProviderAuthorizationError("Twelve Data returned an HTML authorization page")
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as error:
            raise ProviderSchemaError("Twelve Data returned malformed JSON") from error
        if not isinstance(payload, dict):
            raise ProviderSchemaError("Twelve Data returned a non-object JSON payload")
        return payload

    @staticmethod
    def _raise_for_payload_error(payload: Mapping[str, Any], *, now: datetime) -> None:
        if payload.get("status") != "error":
            return
        message = payload.get("message")
        detail = message if isinstance(message, str) and message else "Twelve Data rejected request"
        code = payload.get("code")
        if code in {401, "401"}:
            raise ProviderAuthenticationError(detail)
        if code in {403, "403"}:
            raise ProviderAuthorizationError(detail)
        if code in {429, "429"}:
            raise ProviderRateLimitError(detail, retryable=True)
        if isinstance(code, int) and 500 <= code <= 599:
            raise ProviderUnavailableError(detail, retryable=True)
        if isinstance(code, str) and code.lower() in {"api_key", "invalid_api_key", "unauthorized"}:
            raise ProviderAuthenticationError(detail)
        if isinstance(code, str) and code.lower() in {"forbidden", "permission_denied"}:
            raise ProviderAuthorizationError(detail)
        if isinstance(code, str) and code.lower() in {"too_many_requests", "rate_limit"}:
            raise ProviderRateLimitError(detail, retryable=True)
        _ = now
        raise ProviderSchemaError(detail, code="PROVIDER_REJECTED_REQUEST")

    def _parse_payload(
        self,
        payload: Mapping[str, Any],
        *,
        instrument: TwelveDataInstrument,
        query: BarQuery,
        fetched_at: datetime,
    ) -> tuple[list[MarketBar], list[WarningItem]]:
        meta = payload.get("meta")
        values = payload.get("values")
        if not isinstance(meta, dict) or not isinstance(values, list):
            raise ProviderSchemaError("Twelve Data response is missing meta or values")
        if meta.get("symbol") != instrument.source_symbol:
            raise ProviderSchemaError("Twelve Data response symbol does not match the request")
        if meta.get("interval") != "1day":
            raise ProviderSchemaError("Twelve Data response interval is not 1day")
        currency = meta.get("currency")
        if currency != instrument.currency:
            raise ProviderSchemaError("Twelve Data response currency is not approved USD")
        if len(values) > _MAX_OUTPUT_SIZE:
            raise ProviderSchemaError("Twelve Data response exceeds the requested output bound")

        bars: list[MarketBar] = []
        warnings: list[WarningItem] = []
        previous_date: date | None = None
        for index, raw_row in enumerate(values):
            if not isinstance(raw_row, dict):
                raise ProviderSchemaError("Twelve Data values must contain objects")
            try:
                trading_date = _parse_date(
                    raw_row.get("datetime"), path=f"values[{index}].datetime"
                )
            except ProviderSchemaError as error:
                warnings.append(
                    WarningItem(
                        code="PROVIDER_RECORD_QUARANTINED",
                        message=str(error)[:500],
                        scope=f"values[{index}]",
                        details=_rejection_details(error, raw_row),
                    )
                )
                continue
            if previous_date is not None and trading_date <= previous_date:
                raise ProviderCursorError(
                    "Twelve Data values contain duplicate or descending trading dates",
                    code="INVALID_PAGINATION",
                )
            previous_date = trading_date
            try:
                bar = _market_bar(
                    raw_row,
                    instrument=instrument,
                    trading_date=trading_date,
                    fetched_at=fetched_at,
                )
            except (ProviderSchemaError, UsNormalizationError, ValueError) as error:
                warnings.append(
                    WarningItem(
                        code="PROVIDER_RECORD_QUARANTINED",
                        message=str(error)[:500],
                        scope=f"values[{index}]",
                        details=_rejection_details(error, raw_row),
                    )
                )
                continue
            if query.start <= bar.bar_start < query.end:
                bars.append(bar)
        return bars, warnings

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
            raise ValueError("Twelve Data cursor offset is outside the allowed range")
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
        return ".".join(("twelve-data-v1", _b64encode(body), _b64encode(signature)))

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
                "Twelve Data cursor is malformed", code="INVALID_CURSOR"
            ) from error
        if not isinstance(payload, dict):
            raise ProviderCursorError("Twelve Data cursor is not an object", code="INVALID_CURSOR")
        expected_signature = hmac.new(self._cursor_signing_secret, body, hashlib.sha256).digest()
        offset = payload.get("offset")
        last_bar_id = payload.get("last_bar_id")
        source_watermark = payload.get("source_watermark")
        if (
            version != "twelve-data-v1"
            or not hmac.compare_digest(signature, expected_signature)
            or payload.get("version") != 1
            or payload.get("fingerprint") != fingerprint
            or not isinstance(offset, int)
            or offset < 0
            or offset > _MAX_CURSOR_OFFSET
            or not isinstance(last_bar_id, str)
            or not isinstance(source_watermark, str)
        ):
            raise ProviderCursorError("Twelve Data cursor is not valid", code="INVALID_CURSOR")
        return offset, last_bar_id, source_watermark

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)


def register_us_twelve_data_provider_roles(
    registry: ProviderRegistry, provider: TwelveDataDailyBarsProvider
) -> None:
    """Bind the approved live adapter to the sole US production market role."""

    provider.assert_production_dataset_supported(Dataset.BARS)
    registry.register(provider)
    registry.bind_role(
        TWELVE_DATA_PRIMARY_ROLE,
        provider.provider_id,
        required_dataset=Dataset.BARS,
    )


def _market_bar(
    raw_row: Mapping[str, Any],
    *,
    instrument: TwelveDataInstrument,
    trading_date: date,
    fetched_at: datetime,
) -> MarketBar:
    session = us_equity_session_window(trading_date)
    open_value = _decimal(raw_row.get("open"), path="open")
    high_value = _decimal(raw_row.get("high"), path="high")
    low_value = _decimal(raw_row.get("low"), path="low")
    close_value = _decimal(raw_row.get("close"), path="close")
    volume_raw = raw_row.get("volume")
    volume = None if volume_raw is None else _decimal(volume_raw, path="volume")
    quality_flags = ["VOLUME_MISSING"] if volume is None else []
    provider_record_id = (
        f"{TWELVE_DATA_PROVIDER_ID}:{instrument.source_symbol}:1day:"
        f"{session.open_at.isoformat()}:raw"
    )
    checksum_payload = {
        "symbol": instrument.source_symbol,
        "interval": "1day",
        "datetime": trading_date.isoformat(),
        "open": str(open_value),
        "high": str(high_value),
        "low": str(low_value),
        "close": str(close_value),
        "volume": None if volume is None else str(volume),
        "currency": instrument.currency,
    }
    return MarketBar(
        bar_id=stable_id(
            "bar_us",
            instrument.instrument_id,
            Interval.D1.value,
            session.open_at,
            Adjustment.RAW.value,
            TWELVE_DATA_PROVIDER_ID,
        ),
        instrument_id=instrument.instrument_id,
        canonical_symbol=instrument.canonical_symbol,
        region=Region.US,
        interval=Interval.D1,
        bar_start=session.open_at,
        bar_end=session.close_at,
        trading_date=trading_date,
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=volume,
        currency=instrument.currency,
        adjustment=Adjustment.RAW,
        available_at=fetched_at,
        availability_basis=AvailabilityBasis.FIRST_SEEN,
        source=SourceRef(
            provider_id=TWELVE_DATA_PROVIDER_ID,
            provider_record_id=provider_record_id,
            source_name="Twelve Data",
            source_url=(
                "https://api.twelvedata.com/time_series?"
                f"symbol={instrument.source_symbol}&interval=1day"
            ),
            source_symbol=instrument.source_symbol,
            retrieved_at=fetched_at,
            checksum_sha256=canonical_json_checksum(checksum_payload),
        ),
        quality_flags=quality_flags,
    )


def _instrument_contract(instrument: TwelveDataInstrument, *, fetched_at: datetime) -> Instrument:
    metadata = _INSTRUMENT_METADATA[instrument.source_symbol]
    checksum = canonical_json_checksum(
        {
            "canonical_symbol": instrument.canonical_symbol,
            "source_symbol": instrument.source_symbol,
            "venue_mic": metadata.venue_mic,
            "name": metadata.name,
            "valid_from": metadata.valid_from.isoformat(),
            "currency": instrument.currency,
        }
    )
    return Instrument(
        instrument_id=instrument.instrument_id,
        canonical_symbol=instrument.canonical_symbol,
        region=Region.US,
        venue_mic=metadata.venue_mic,
        local_symbol=instrument.source_symbol,
        name=metadata.name,
        asset_class=AssetClass.ETF,
        currency=instrument.currency,
        timezone="America/New_York",
        status=InstrumentStatus.ACTIVE,
        valid_from=metadata.valid_from,
        source=SourceRef(
            provider_id=TWELVE_DATA_PROVIDER_ID,
            provider_record_id=f"{TWELVE_DATA_PROVIDER_ID}:{instrument.source_symbol}:instrument",
            source_name="Twelve Data",
            source_symbol=instrument.source_symbol,
            retrieved_at=fetched_at,
            checksum_sha256=checksum,
        ),
    )


def _parse_date(value: object, *, path: str) -> date:
    if not isinstance(value, str):
        raise ProviderSchemaError(f"Twelve Data {path} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ProviderSchemaError(f"Twelve Data {path} must be an ISO date") from error


def _decimal(value: object, *, path: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ProviderSchemaError(f"Twelve Data {path} must be a decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ProviderSchemaError(f"Twelve Data {path} must be a decimal") from error
    if not parsed.is_finite():
        raise ProviderSchemaError(f"Twelve Data {path} must be finite")
    return parsed


def _ensure_unique_bar_ids(bars: Sequence[MarketBar]) -> None:
    seen: set[str] = set()
    for bar in bars:
        if bar.bar_id in seen:
            raise ProviderCursorError(
                "Twelve Data response contains a duplicate bar", code="INVALID_PAGINATION"
            )
        seen.add(bar.bar_id)


def _retry_after_seconds(response: httpx.Response, *, now: datetime) -> int | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value).astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            return None
        return max(0, int((retry_at - now).total_seconds()))


def _retry_delay_seconds(error: ProviderError, *, attempt: int) -> float:
    if error.retry_after_seconds is not None:
        return float(min(error.retry_after_seconds, _MAX_RETRY_DELAY_SECONDS))
    return float(min(2**attempt, _MAX_RETRY_DELAY_SECONDS))


def _rejection_details(error: Exception, raw_row: Mapping[str, Any]) -> dict[str, object]:
    """Keep a bounded, credential-safe row fragment for durable quarantine evidence."""

    forbidden_keys = {"apikey", "api_key", "authorization", "cookie", "token", "password"}
    fields = sorted(key for key in raw_row if key.lower() not in forbidden_keys)
    raw_datetime = raw_row.get("datetime")
    return {
        "rejection": {
            "error_code": (
                error.code if isinstance(error, ProviderError) else "NORMALIZATION_ERROR"
            ),
            "redacted_payload": {
                "datetime": raw_datetime[:64] if isinstance(raw_datetime, str) else None,
                "fields": fields,
            },
        }
    }


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _with_padding(value: str) -> str:
    return value + "=" * (-len(value) % 4)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)

# 宏观数据与新闻平台工程执行规范

**版本：** v1.0

**状态：** 实习生执行版

**日期：** 2026-07-23

**形态：** 从零开发、纯后端、无图形界面

**人员：** 实习生 A（A股 + 港股）、实习生 B（美股 + 公共底座）、项目负责人（宏观总编 LLM）

---

## 1. 目标、边界与完成标准

本项目从零建设一个独立的数据平台，为宏观总编 LLM 提供 A股、港股、美股、宏观数据和每日新闻。平台只提供事实、来源、时间和数据质量，不生成交易观点。

```text
外部数据源
    ↓
Provider Adapter
    ↓
标准化 → 校验 → 去重 → 入库
    ↓
PostgreSQL
    ↓
REST API
    ↓
宏观总编 LLM
```

### 1.1 必须交付

- 三个市场的证券主数据、交易日历、日线行情和核心市场指标。
- 中国、香港、美国的宏观、利率、汇率和重要跨资产数据。
- 三个区域的每日新闻、政策、公告、宏观发布和重要公司事件。
- 可重放、可追溯、支持历史 `as_of` 的数据库。
- 统一 Provider Protocol 和 REST API。
- 面向宏观总编的 `/v1/editor/context`。
- 完整离线测试、真实数据库测试、在线冒烟测试和数据质量报告。
- Docker 部署、运行手册、数据授权登记和故障恢复说明。

### 1.2 明确不做

- 不做桌面或 Web 图形界面。
- 不接券商，不下单，不管理仓位。
- 不生成买卖信号、目标价、仓位、止损或收益承诺。
- 不实现宏观总编 prompt、观点合成和文章发布。
- 首期不做 tick、Level 2、盘口和高频交易数据。
- 不把供应商情绪标签升级为平台自己的交易因子。

### 1.3 第一阶段完成标准

只有同时满足以下条件才算完成：

1. 三个区域均能通过同一 API 查询。
2. 宏观总编不需要了解任何供应商字段。
3. 任意记录都能追溯至 provider、上游记录 ID 和抓取批次。
4. 历史查询未来数据泄漏为零。
5. 同一任务重跑不会产生重复数据。
6. 连续五个交易日自动生成三个 session，且达到第 14 节质量门槛。
7. 所有 CI、授权检查和交叉评审通过。

---

## 2. 技术栈与仓库结构

### 2.1 技术栈

| 组件 | 选择 |
|---|---|
| 语言 | Python 3.12+ |
| API | FastAPI + Pydantic v2 |
| HTTP | httpx |
| 数据库 | PostgreSQL |
| ORM | SQLAlchemy 2 |
| Migration | Alembic |
| 调度 | 单独 worker + 持久化 job state + PostgreSQL advisory lock |
| 缓存 | 首期不强制；确有瓶颈再引入 Redis |
| 测试 | pytest + pytest-asyncio + testcontainers/respx |
| 质量 | ruff + mypy strict |
| 包管理 | uv + 锁文件 |
| 部署 | Docker Compose |
| 观测 | JSON 日志 + Prometheus 指标 |

API 和 worker 是两个进程：API 只读已入库数据；worker 负责抓取和入库。API 请求期间禁止临时调用外部数据源。

### 2.2 仓库结构

```text
macro-data-platform/
├── pyproject.toml
├── uv.lock
├── .env.example
├── docker-compose.yml
├── alembic.ini
├── migrations/
│   └── versions/
├── src/macro_platform/
│   ├── contracts/                 # 唯一公共入参/出参
│   │   ├── common.py
│   │   ├── market.py
│   │   ├── macro.py
│   │   ├── news.py
│   │   ├── provider.py
│   │   └── editor.py
│   ├── providers/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── cn/
│   │   ├── hk/
│   │   └── us/
│   ├── normalization/
│   │   ├── common/
│   │   ├── cn_hk/
│   │   └── us/
│   ├── storage/
│   │   ├── models.py
│   │   ├── repositories.py
│   │   └── unit_of_work.py
│   ├── services/
│   │   ├── market_service.py
│   │   ├── macro_service.py
│   │   ├── news_service.py
│   │   └── editor_context_service.py
│   ├── api/
│   │   ├── app.py
│   │   ├── dependencies.py
│   │   ├── exception_handlers.py
│   │   └── routes/
│   ├── jobs/
│   │   ├── scheduler.py
│   │   ├── runner.py
│   │   └── definitions/
│   ├── observability/
│   └── config.py
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── e2e/
│   ├── live/
│   ├── quality/
│   ├── fixtures/{common,cn,hk,us}/
│   └── golden/
├── docs/
│   ├── adr/
│   ├── data-sources/
│   └── runbooks/
└── .github/
    ├── CODEOWNERS
    ├── pull_request_template.md
    └── workflows/
```

`contracts/` 是唯一事实来源。区域 provider 不得各自创建另一套公共 DTO。

---

## 3. 全局数据纪律

### 3.1 严格模型

所有公共 Pydantic 模型统一继承：

```python
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        populate_by_name=True,
    )


DecimalValue = Annotated[
    Decimal,
    Field(max_digits=38, decimal_places=18),
]
```

规定：

- 未声明字段直接校验失败，禁止静默忽略拼写错误。
- 价格、金额、比例和数量使用 `Decimal`，API 中序列化为字符串。
- 缺失只能是 `null`，禁止 `--`、`N/A`、`-999` 和空字符串哨兵值。
- 禁止用二进制浮点数作为数据库持久化标准。

### 3.2 标识符

```text
instrument_id      平台生成、终身不变，例如 ins_01J...
canonical_symbol   <MIC>:<LOCAL_SYMBOL>
source_symbol      供应商原始代码
```

示例：

```text
XSHG:600519
XSHE:000001
XHKG:00700
XNAS:AAPL
XNYS:BRK.B
```

证券更名或换代码时，`instrument_id` 不变；canonical symbol 与供应商 alias 使用
`valid_from/valid_to` 管理，有效区间为 `[valid_from, valid_to)`（`valid_to=null` 表示未结束）。

非证券序列 ID：

```text
macro:CN:NBS:CPI_YOY
macro:US:BLS:CPI_ALL_ITEMS
market:HK:flow.southbound.net_buy
market:US:breadth.advancers
rate:US:UST10Y
fx:GLOBAL:USDCNH
```

### 3.3 时间

所有 timestamp 必须带时区，数据库和 API 统一 UTC `Z`。所有查询区间统一左闭右开：

```text
start <= record_time < end
```

时间字段语义：

```text
event_at/source_published_at   业务事件或来源声明时间
observed_at                    行情或数值代表时点
first_seen_at                  平台或可信供应商首次看到时间
available_at                   历史查询允许使用的最早时间
retrieved_at                   本次抓取完成时间
ingested_at                    标准记录入库时间
```

每个历史查询必须执行：

```text
record.available_at <= request.as_of
```

无法证明历史分发时间时，`available_at = first_seen_at`，并设置 `availability_basis=first_seen`。禁止把来源文章日期直接当成可回测时间。

只有日期精度的数据使用 `date` 和 `time_precision=date`，不得伪造午夜 timestamp。

### 3.4 数值与单位

- `value="5.2", unit="percent"` 表示 5.2%。
- 25bp 规范化为 `value="0.25", unit="percent"`。
- 1.2 亿元规范化为 `value="120000000", currency="CNY"`。
- 必须保留 `raw_value`、`raw_unit` 和 `normalization_rule`，至少在审计/raw 层保存。
- level、同比、环比、累计值使用不同 `transformation/measure_type`。
- adapter 不得为展示提前四舍五入。

---

## 4. 公共输出模型

以下模型必须原样进入 `contracts/`；实现时允许拆文件，不允许改变字段语义。

### 4.1 枚举与来源

```python
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, HttpUrl


class Region(StrEnum):
    CN = "CN"
    HK = "HK"
    US = "US"
    GLOBAL = "GLOBAL"


class AssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"
    FUTURE = "future"
    FX = "fx"
    RATE = "rate"
    COMMODITY = "commodity"


class AvailabilityBasis(StrEnum):
    PROVIDER_DISSEMINATED = "provider_disseminated"
    FIRST_SEEN = "first_seen"
    EXCHANGE_PUBLISHED = "exchange_published"
    INFERRED = "inferred"


class SourceRef(StrictModel):
    provider_id: str
    provider_record_id: Annotated[str, Field(min_length=1, max_length=256)]
    source_name: Annotated[str, Field(min_length=1, max_length=256)]
    source_url: HttpUrl | None = None
    source_symbol: str | None = None
    retrieved_at: AwareDatetime
    provider_updated_at: AwareDatetime | None = None
    checksum_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class UsageRights(StrictModel):
    storage_allowed: bool
    internal_analysis_allowed: bool
    external_llm_allowed: bool
    embedding_allowed: bool
    redistribution_allowed: bool
    content_expires_at: AwareDatetime | None = None


class WarningItem(StrictModel):
    code: str
    message: Annotated[str, Field(min_length=1, max_length=500)]
    scope: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
```

`UsageRights` 是 v1 兼容元数据；内部个人使用运行时不以这些字段阻断采集、保存、
EditorContext、LLM、embedding 或报告引用。

### 4.2 Instrument

```python
class InstrumentStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELISTED = "delisted"
    UNKNOWN = "unknown"


class Instrument(StrictModel):
    instrument_id: str
    canonical_symbol: str
    region: Region
    venue_mic: str
    local_symbol: str
    name: str
    name_en: str | None = None
    asset_class: AssetClass
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    timezone: str
    status: InstrumentStatus
    listed_on: date | None = None
    delisted_on: date | None = None
    lot_size: DecimalValue | None = None
    valid_from: date
    valid_to: date | None = None
    source: SourceRef
```

### 4.3 MarketBar

```python
class Interval(StrEnum):
    D1 = "1d"
    W1 = "1w"
    MO1 = "1mo"


class Adjustment(StrEnum):
    RAW = "raw"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN = "total_return"


class MarketBar(StrictModel):
    bar_id: str
    instrument_id: str
    canonical_symbol: str
    region: Region
    interval: Interval
    bar_start: AwareDatetime
    bar_end: AwareDatetime
    trading_date: date
    open: DecimalValue
    high: DecimalValue
    low: DecimalValue
    close: DecimalValue
    volume: DecimalValue | None = Field(default=None, ge=0)
    turnover: DecimalValue | None = Field(default=None, ge=0)
    vwap: DecimalValue | None = None
    currency: str
    adjustment: Adjustment
    adjustment_as_of: AwareDatetime | None = None
    available_at: AwareDatetime
    availability_basis: AvailabilityBasis
    source: SourceRef
    quality_flags: list[str] = Field(default_factory=list)
```

校验：`bar_start < bar_end`、`low <= open/close <= high`、volume/turnover 非负；非 raw 复权必须有 `adjustment_as_of`。首期所有行情 provider 必须支持 `raw + 1d`。

### 4.4 MarketObservation 与 Snapshot

```python
class ScopeType(StrEnum):
    INSTRUMENT = "instrument"
    MARKET = "market"
    EXCHANGE = "exchange"
    SECTOR = "sector"


class MarketObservation(StrictModel):
    observation_id: str
    region: Region
    scope_type: ScopeType
    scope_id: str
    metric_code: str
    value: DecimalValue | None
    unit: str
    currency: str | None = None
    period_start: AwareDatetime
    period_end: AwareDatetime
    observed_at: AwareDatetime
    available_at: AwareDatetime
    availability_basis: AvailabilityBasis
    dimensions: dict[str, str] = Field(default_factory=dict)
    source: SourceRef
    quality_flags: list[str] = Field(default_factory=list)


class MarketSnapshot(StrictModel):
    instrument_id: str
    canonical_symbol: str
    region: Region
    price_time: AwareDatetime
    last: DecimalValue
    previous_close: DecimalValue | None = None
    change: DecimalValue | None = None
    change_pct: DecimalValue | None = None
    volume: DecimalValue | None = None
    turnover: DecimalValue | None = None
    currency: str
    available_at: AwareDatetime
    source_records: list[SourceRef]
```

`MarketSnapshot` 由服务层从已入库数据生成，不要求 provider 单独抓一套快照。

### 4.5 宏观模型

```python
class Frequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    IRREGULAR = "irregular"


class RevisionPolicy(StrEnum):
    LATEST_AS_OF = "latest_as_of"
    FIRST_RELEASE = "first_release"
    ALL_VINTAGES = "all_vintages"


class MacroSeries(StrictModel):
    series_id: str
    region: Region
    authority: str
    code: str
    name: str
    description: str | None = None
    frequency: Frequency
    unit: str
    transformation: Literal["level", "mom", "qoq", "yoy", "annualized", "index"]
    seasonal_adjustment: Literal["adjusted", "not_adjusted", "unknown"]
    source: SourceRef


class MacroObservation(StrictModel):
    observation_id: str
    series_id: str
    region: Region
    period_start: date
    period_end: date
    value: DecimalValue | None
    unit: str
    transformation: str
    released_at: AwareDatetime | None
    available_at: AwareDatetime
    availability_basis: AvailabilityBasis
    vintage_id: str
    revision_no: int = Field(ge=0)
    value_status: Literal["estimate", "preliminary", "final"]
    supersedes_observation_id: str | None = None
    source: SourceRef
    quality_flags: list[str] = Field(default_factory=list)


class MacroRelease(StrictModel):
    release_id: str
    series_id: str
    region: Region
    release_name: str
    scheduled_at: AwareDatetime | None = None
    scheduled_date: date | None = None
    time_precision: Literal["instant", "date"] = "instant"
    released_at: AwareDatetime | None = None
    available_at: AwareDatetime
    period_start: date
    period_end: date
    actual: DecimalValue | None = None
    consensus: DecimalValue | None = None
    previous: DecimalValue | None = None
    unit: str
    status: Literal["scheduled", "released", "delayed", "cancelled"]
    source: SourceRef
```

每次宏观修订新增 vintage，禁止原位覆盖历史值。

### 4.6 新闻模型

```python
class SourceTier(StrEnum):
    OFFICIAL = "official"
    LICENSED_MEDIA = "licensed_media"
    RESEARCH = "research"
    SOCIAL = "social"
    OTHER = "other"


class ContentMode(StrEnum):
    HEADLINE = "headline"
    SNIPPET = "snippet"
    FULL_TEXT = "full_text"


class EntityRef(StrictModel):
    entity_type: Literal[
        "instrument", "company", "country", "sector", "person",
        "organization", "commodity", "currency"
    ]
    entity_id: str
    mention: str | None = None
    confidence: DecimalValue = Field(ge=0, le=1)


class VendorAnnotation(StrictModel):
    provider_id: str
    annotation_type: Literal["sentiment", "importance", "event", "attention"]
    label: str | None = None
    score: DecimalValue | None = None
    scale_min: DecimalValue | None = None
    scale_max: DecimalValue | None = None
    model_version: str | None = None


class NewsEvent(StrictModel):
    news_id: str
    cluster_id: str | None = None
    supersedes_news_id: str | None = None
    status: Literal["active", "corrected", "retracted"] = "active"
    title: Annotated[str, Field(min_length=1, max_length=1000)]
    summary: Annotated[str, Field(max_length=10000)] | None = None
    body: str | None = None
    content_mode: ContentMode
    language: str
    source_name: str
    source_tier: SourceTier
    canonical_url: HttpUrl | None = None
    published_at: AwareDatetime | None = None
    published_date: date | None = None
    time_precision: Literal["instant", "date"] = "instant"
    first_seen_at: AwareDatetime
    available_at: AwareDatetime
    availability_basis: AvailabilityBasis
    regions: list[Region] = Field(min_length=1)
    entities: list[EntityRef] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    vendor_annotations: list[VendorAnnotation] = Field(default_factory=list)
    content_hash_sha256: str
    usage_rights: UsageRights
    source: SourceRef
    quality_flags: list[str] = Field(default_factory=list)
```

规定：厂商标签只能进入 `vendor_annotations`；跨来源相似新闻共享 `cluster_id`，但各自保留 news ID、来源和时间；body 是否为空仅由实际可用内容决定。

---

## 5. Provider 精确入参、出参与异常

### 5.1 通用上下文与分页输出

```python
from typing import Generic, Protocol, TypeVar
from uuid import UUID

T = TypeVar("T", bound=StrictModel)


class FetchContext(StrictModel):
    request_id: UUID
    as_of: AwareDatetime
    deadline_at: AwareDatetime


class ProviderPage(StrictModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    source_watermark: str | None = None
    fetched_at: AwareDatetime
    complete: bool
    warnings: list[WarningItem] = Field(default_factory=list)


class ProviderCapabilities(StrictModel):
    provider_id: str
    regions: set[Region]
    datasets: set[str]
    intervals: set[Interval] = Field(default_factory=set)
    max_page_size: int = Field(ge=1, le=10000)
    supports_point_in_time: bool
    supports_revisions: bool
    supports_full_text: bool
    external_llm_allowed: bool


class ProviderHealth(StrictModel):
    provider_id: str
    status: Literal["ok", "degraded", "down", "not_configured"]
    checked_at: AwareDatetime
    latency_ms: int = Field(ge=0)
    message: str | None = None
```

### 5.2 查询入参

```python
class InstrumentQuery(StrictModel):
    regions: set[Region] = Field(min_length=1)
    venues: set[str] = Field(default_factory=set)
    asset_classes: set[AssetClass] = Field(default_factory=set)
    active_on: date | None = None
    modified_since: AwareDatetime | None = None
    cursor: str | None = None
    limit: int = Field(default=500, ge=1, le=1000)


class BarQuery(StrictModel):
    instrument_ids: list[str] = Field(min_length=1, max_length=100)
    interval: Interval
    start: AwareDatetime
    end: AwareDatetime
    adjustment: Adjustment = Adjustment.RAW
    as_of: AwareDatetime
    cursor: str | None = None
    limit: int = Field(default=1000, ge=1, le=5000)


class MarketObservationQuery(StrictModel):
    regions: set[Region] = Field(min_length=1)
    metric_codes: list[str] = Field(min_length=1, max_length=50)
    scope_ids: list[str] = Field(default_factory=list, max_length=100)
    start: AwareDatetime
    end: AwareDatetime
    as_of: AwareDatetime
    cursor: str | None = None
    limit: int = Field(default=1000, ge=1, le=5000)


class MacroSeriesQuery(StrictModel):
    regions: set[Region] = Field(min_length=1)
    series_ids: list[str] = Field(default_factory=list, max_length=100)
    cursor: str | None = None
    limit: int = Field(default=500, ge=1, le=1000)


class MacroObservationQuery(StrictModel):
    series_ids: list[str] = Field(min_length=1, max_length=100)
    period_from: date
    period_to: date
    as_of: AwareDatetime
    revision_policy: RevisionPolicy = RevisionPolicy.LATEST_AS_OF
    cursor: str | None = None
    limit: int = Field(default=1000, ge=1, le=5000)


class MacroReleaseQuery(StrictModel):
    regions: set[Region] = Field(min_length=1)
    scheduled_from: AwareDatetime
    scheduled_to: AwareDatetime
    as_of: AwareDatetime
    cursor: str | None = None
    limit: int = Field(default=500, ge=1, le=1000)


class NewsQuery(StrictModel):
    regions: set[Region] = Field(min_length=1)
    published_from: AwareDatetime
    published_to: AwareDatetime
    as_of: AwareDatetime
    entity_ids: list[str] = Field(default_factory=list, max_length=100)
    topics: list[str] = Field(default_factory=list, max_length=50)
    languages: set[str] = Field(default_factory=set)
    source_tiers: set[SourceTier] = Field(default_factory=set)
    include_superseded: bool = False
    content_mode: ContentMode = ContentMode.SNIPPET
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=500)
```

所有 start/end 使用左闭右开且必须 `start < end`。Provider 收到模型后不得修改输入对象。

### 5.3 Protocol

```python
class BaseProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    async def healthcheck(self) -> ProviderHealth: ...
    async def aclose(self) -> None: ...


class MarketDataProvider(BaseProvider, Protocol):
    async def fetch_instruments(
        self, query: InstrumentQuery, context: FetchContext
    ) -> ProviderPage[Instrument]: ...

    async def fetch_bars(
        self, query: BarQuery, context: FetchContext
    ) -> ProviderPage[MarketBar]: ...

    async def fetch_market_observations(
        self, query: MarketObservationQuery, context: FetchContext
    ) -> ProviderPage[MarketObservation]: ...


class MacroDataProvider(BaseProvider, Protocol):
    async def fetch_macro_series(
        self, query: MacroSeriesQuery, context: FetchContext
    ) -> ProviderPage[MacroSeries]: ...

    async def fetch_macro_observations(
        self, query: MacroObservationQuery, context: FetchContext
    ) -> ProviderPage[MacroObservation]: ...

    async def fetch_macro_releases(
        self, query: MacroReleaseQuery, context: FetchContext
    ) -> ProviderPage[MacroRelease]: ...


class NewsProvider(BaseProvider, Protocol):
    async def fetch_news(
        self, query: NewsQuery, context: FetchContext
    ) -> ProviderPage[NewsEvent]: ...
```

两位实习生必须实现同一 Protocol。禁止返回 `{"success": false}` 字典；失败必须抛统一异常：

```python
class ProviderError(Exception):
    code: str
    retryable: bool
    retry_after_seconds: int | None


class ProviderAuthenticationError(ProviderError): ...
class ProviderAuthorizationError(ProviderError): ...
class ProviderRateLimitError(ProviderError): ...
class ProviderUnavailableError(ProviderError): ...
class ProviderTimeoutError(ProviderError): ...
class ProviderSchemaError(ProviderError): ...
class ProviderCursorError(ProviderError): ...
class UnsupportedCapabilityError(ProviderError): ...
```

### 5.4 抓取任务入参与出参

```python
class IngestJobRequest(StrictModel):
    provider_role: str
    dataset: Literal[
        "instruments", "bars", "market_observations",
        "macro_observations", "macro_releases", "news"
    ]
    regions: set[Region]
    start: AwareDatetime
    end: AwareDatetime
    as_of: AwareDatetime
    cursor: str | None = None
    dry_run: bool = False
    force: bool = False


class IngestJobResult(StrictModel):
    run_id: UUID
    status: Literal["succeeded", "partial", "failed", "retry_wait"]
    provider_role: str
    dataset: str
    started_at: AwareDatetime
    finished_at: AwareDatetime
    records_fetched: int = Field(ge=0)
    records_accepted: int = Field(ge=0)
    records_rejected: int = Field(ge=0)
    records_inserted: int = Field(ge=0)
    records_updated: int = Field(ge=0)
    next_cursor: str | None = None
    source_watermark: str | None = None
    error_code: str | None = None
    retry_after_seconds: int | None = None
    warnings: list[WarningItem] = Field(default_factory=list)
```

任务只有在数据事务和 watermark 同时提交后才能标记成功。任务崩溃重跑必须幂等。

---

## 6. REST API 精确契约

### 6.1 路由

| 方法 | 路径 | 入参模型 | 出参 data |
|---|---|---|---|
| `GET` | `/health/live` | 无 | 进程状态 |
| `GET` | `/health/ready` | 无 | 数据库/必要依赖状态 |
| `GET` | `/v1/operations/worker-readiness` | Bearer token | Worker 配置与关键表 readiness |
| `GET` | `/v1/operations/daily-workflows/{report_date}` | Bearer token | 脱敏工作流状态与审计链 |
| `POST` | `/v1/operations/daily-reports/{report_id}/delivery-retry` | DeliveryRetryRequest + Bearer token + X-Request-ID | 幂等投递恢复结果 |
| `GET` | `/v1/meta/capabilities` | 可选 region | ProviderCapabilities 列表 |
| `GET` | `/v1/instruments` | InstrumentQuery | `items: Instrument[]` |
| `GET` | `/v1/market/bars` | BarQuery | `items: MarketBar[]` |
| `GET` | `/v1/market/snapshots` | instrument_id[], as_of | `items: MarketSnapshot[]` |
| `GET` | `/v1/market/observations` | MarketObservationQuery | `items: MarketObservation[]` |
| `GET` | `/v1/macro/series` | MacroSeriesQuery | `items: MacroSeries[]` |
| `GET` | `/v1/macro/observations` | MacroObservationQuery | `items: MacroObservation[]` |
| `GET` | `/v1/macro/releases` | MacroReleaseQuery | `items: MacroRelease[]` |
| `GET` | `/v1/news` | NewsQuery | `items: NewsEvent[]` |
| `POST` | `/v1/editor/context` | EditorContextRequest | EditorContext |

数组 query 参数使用重复参数：

```text
?instrument_id=ins_001&instrument_id=ins_002
```

禁止自行用逗号拆分。`as_of` 未传时 API 在收到请求时冻结当前 UTC 并返回；回测调用必须显式传。

### 6.2 成功 Envelope

```python
T = TypeVar("T")


class PageMeta(StrictModel):
    limit: int
    has_more: bool
    next_cursor: str | None = None


class SuccessEnvelope(StrictModel, Generic[T]):
    request_id: UUID
    api_version: Literal["v1"] = "v1"
    as_of: AwareDatetime
    snapshot_at: AwareDatetime
    data: T
    page: PageMeta | None = None
    warnings: list[WarningItem] = Field(default_factory=list)
```

列表一律放在 `data.items`，禁止一会儿返回数组、一会儿用 `results`。

### 6.3 失败 Envelope

```python
class ErrorDetail(StrictModel):
    location: list[str | int]
    message: str
    error_type: str


class ApiError(StrictModel):
    code: str
    message: str
    retryable: bool
    retry_after_seconds: int | None = None
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorEnvelope(StrictModel):
    request_id: UUID
    api_version: Literal["v1"] = "v1"
    error: ApiError
```

| HTTP | code |
|---:|---|
| 400 | `INVALID_CURSOR`, `INVALID_RANGE` |
| 401 | `UNAUTHENTICATED` |
| 403 | `FORBIDDEN`, `LICENSE_RESTRICTION` |
| 404 | `RESOURCE_NOT_FOUND` |
| 409 | `POINT_IN_TIME_UNAVAILABLE` |
| 422 | `VALIDATION_ERROR`, `UNSUPPORTED_QUERY` |
| 429 | `RATE_LIMITED` |
| 500 | `INTERNAL_ERROR` |
| 503 | `DATASET_UNAVAILABLE`, `DEPENDENCY_UNAVAILABLE` |

失败响应示例：

```json
{
  "request_id": "8418c7d8-69ca-4cc1-b061-5b36d20f957e",
  "api_version": "v1",
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "end must be later than start",
    "retryable": false,
    "retry_after_seconds": null,
    "details": [
      {
        "location": ["query", "end"],
        "message": "must be greater than start",
        "error_type": "value_error.range"
      }
    ]
  }
}
```

### 6.4 分页

- 禁止 offset，只用不透明 cursor。
- cursor 绑定过滤条件 hash、as_of、snapshot_at、最后排序键和 API 版本。
- 修改过滤条件后继续使用旧 cursor，返回 `400 INVALID_CURSOR`。
- 首次请求冻结 snapshot_at，后续页面沿用，避免新入库记录造成重复/漏项。
- 不承诺 total。

固定排序：

```text
bars                 bar_end ASC, instrument_id ASC, bar_id ASC
market observations  observed_at ASC, observation_id ASC
macro observations   period_end ASC, series_id ASC, available_at ASC
macro releases       scheduled_at ASC, release_id ASC
news                  published_at DESC, news_id DESC
```

### 6.5 MarketBar 成功响应示例

```json
{
  "request_id": "2293f370-e623-4dbf-819d-34231756cf03",
  "api_version": "v1",
  "as_of": "2026-07-23T08:00:00Z",
  "snapshot_at": "2026-07-23T08:00:01Z",
  "data": {
    "items": [
      {
        "bar_id": "bar_50a7d1",
        "instrument_id": "ins_hk_00700",
        "canonical_symbol": "XHKG:00700",
        "region": "HK",
        "interval": "1d",
        "bar_start": "2026-07-22T01:30:00Z",
        "bar_end": "2026-07-22T08:00:00Z",
        "trading_date": "2026-07-22",
        "open": "500.000000",
        "high": "505.000000",
        "low": "496.000000",
        "close": "502.500000",
        "volume": "12000000",
        "turnover": "6010000000.00",
        "vwap": "500.833333",
        "currency": "HKD",
        "adjustment": "raw",
        "adjustment_as_of": null,
        "available_at": "2026-07-22T08:00:05Z",
        "availability_basis": "provider_disseminated",
        "source": {
          "provider_id": "hk_market_primary",
          "provider_record_id": "record-123",
          "source_name": "licensed-market-source",
          "source_url": null,
          "source_symbol": "700.HK",
          "retrieved_at": "2026-07-22T08:00:08Z",
          "provider_updated_at": "2026-07-22T08:00:05Z",
          "checksum_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        },
        "quality_flags": []
      }
    ]
  },
  "page": {"limit": 100, "has_more": false, "next_cursor": null},
  "warnings": []
}
```

---

## 7. EditorContext 入参和出参

### 7.1 请求模型

```python
class MarketContextSpec(StrictModel):
    instrument_ids: list[str] = Field(default_factory=list, max_length=100)
    lookback_sessions: int = Field(default=5, ge=2, le=30)
    metric_codes: list[str] = Field(default_factory=list, max_length=50)


class MacroContextSpec(StrictModel):
    series_ids: list[str] = Field(default_factory=list, max_length=100)
    lookback_days: int = Field(default=120, ge=1, le=3660)
    upcoming_days: int = Field(default=7, ge=0, le=90)
    revision_policy: RevisionPolicy = RevisionPolicy.LATEST_AS_OF


class NewsContextSpec(StrictModel):
    lookback_hours: int = Field(default=24, ge=1, le=744)
    topics: list[str] = Field(default_factory=list, max_length=50)
    source_tiers: set[SourceTier] = Field(default_factory=set)
    languages: set[str] = Field(default_factory=set)
    max_items: int = Field(default=100, ge=1, le=500)
    max_per_cluster: int = Field(default=1, ge=1, le=10)
    content_mode: Literal["headline", "snippet"] = "snippet"


class EditorContextRequest(StrictModel):
    as_of: AwareDatetime | None = None
    regions: set[Region] = Field(min_length=1)
    preset_id: str = "daily_macro_v1"
    market: MarketContextSpec = Field(default_factory=MarketContextSpec)
    macro: MacroContextSpec = Field(default_factory=MacroContextSpec)
    news: NewsContextSpec = Field(default_factory=NewsContextSpec)
    require_point_in_time: bool = True
    fail_on_incomplete: bool = False
```

请求示例：

```json
{
  "as_of": "2026-07-23T08:00:00Z",
  "regions": ["CN", "HK", "US"],
  "preset_id": "daily_macro_v1",
  "market": {
    "instrument_ids": ["ins_cn_csi300", "ins_hk_hsi", "ins_us_spx"],
    "lookback_sessions": 5,
    "metric_codes": ["flow.northbound.net_buy", "breadth.advancers"]
  },
  "macro": {
    "series_ids": ["macro:CN:NBS:CPI_YOY", "macro:US:BLS:CPI_ALL_ITEMS"],
    "lookback_days": 120,
    "upcoming_days": 7,
    "revision_policy": "latest_as_of"
  },
  "news": {
    "lookback_hours": 24,
    "topics": ["monetary_policy", "economic_data"],
    "source_tiers": ["official", "licensed_media"],
    "languages": ["zh-CN", "en-US"],
    "max_items": 100,
    "max_per_cluster": 1,
    "content_mode": "snippet"
  },
  "require_point_in_time": true,
  "fail_on_incomplete": false
}
```

### 7.2 响应模型

```python
class CoverageItem(StrictModel):
    dataset: str
    region: Region
    status: Literal["complete", "partial", "stale", "unavailable"]
    record_count: int = Field(ge=0)
    newest_available_at: AwareDatetime | None = None
    providers: list[str]
    reasons: list[str] = Field(default_factory=list)


class ResolvedContextSelection(StrictModel):
    preset_id: str
    preset_version: str
    instrument_ids: list[str]
    series_ids: list[str]
    metric_codes: list[str]
    topic_taxonomy_version: str


class EditorContext(StrictModel):
    context_id: str
    context_version: Literal["1.0"]
    generated_at: AwareDatetime
    as_of: AwareDatetime
    resolved_selection: ResolvedContextSelection
    market_snapshots: list[MarketSnapshot]
    market_bars: list[MarketBar]
    market_observations: list[MarketObservation]
    macro_observations: list[MacroObservation]
    macro_releases: list[MacroRelease]
    news_events: list[NewsEvent]
    coverage: list[CoverageItem]
    data_fingerprint_sha256: str
```

相同请求、相同 as_of、相同数据库快照必须得到相同 `data_fingerprint_sha256`。内部个人使用不按旧 rights 标记剔除数据；`fail_on_incomplete=true` 且必需数据 unavailable 时返回 503。

---

## 8. 数据库与幂等规则

### 8.1 表

```text
instruments
instrument_aliases
market_bars
market_observations
macro_series
macro_observations
macro_releases
news_events
news_event_regions
news_event_entities
news_event_topics
provider_capabilities
provider_runs
ingest_rejections
ingest_page_commits
ingest_audits
job_watermarks
context_builds
```

### 8.2 关键唯一约束

```text
instrument_aliases(provider_id, source_symbol, valid_from)
market_bars(instrument_id, interval, bar_start, adjustment, source.provider_id)
market_observations(observation_id)
macro_observations(series_id, period_end, vintage_id, source.provider_id)
macro_releases(release_id)
news_events(news_id)
provider_runs(run_id)
ingest_page_commits(provider_role, dataset, region, page_fingerprint)
```

`SourceRef` 在数据库拆成独立列或 JSONB 均可，但 provider ID、provider record ID、checksum、retrieved_at 必须可索引和查询。
`ingest_page_commits` 是同页重试的事务性 reservation：仅 reservation 与业务记录同一事务提交后，
才更新 `job_watermarks`。`ingest_audits` 保存不支持历史 PIT 与原始时区等可审计证据；拒绝路径
不得因外层业务事务回滚而丢失该证据。

### 8.3 关键索引

```text
market_bars(instrument_id, bar_end DESC)
market_observations(metric_code, available_at DESC)
macro_observations(series_id, period_end, available_at DESC)
macro_releases(region, scheduled_at)
news_events(published_at DESC, news_id)
news_events(cluster_id)
provider_runs(provider_role, started_at DESC)
```

### 8.4 写入纪律

- Provider 不直接写数据库。
- pipeline 先标准化和校验，再由 repository 写入。
- 相同 raw record 重跑产生相同 canonical ID。
- 数据事务与 watermark 同一事务提交。
- 非法记录进入 quarantine，保存错误码、脱敏原始片段、run ID。
- 新闻更正和宏观修订新增版本，不物理覆盖。
- 大规模数据回填与 schema migration 分开。

---

## 9. 最低数据覆盖与调度

### 9.1 A股/中国

- 上证综指、深证成指、沪深300、中证500、创业板指、科创50。
- 日 OHLCV、成交额、涨跌幅、上涨/下跌家数和主要行业表现。
- 可合法稳定取得的沪深港通、两融、估值和资金流。
- 人民币、国债收益率及中国相关商品。
- GDP、CPI、PPI、PMI、社融、M2、进出口、工业增加值、固定资产投资和房地产核心指标。
- 央行、统计、财政、监管、交易所发布以及每日新闻/公告。

### 9.2 港股

- 恒生指数、恒生国企、恒生科技。
- 核心股票日 OHLCV、成交额、行业表现和市场宽度。
- 南向资金、USD/HKD、CNH 和香港关键利率。
- 交易所公告、公司行动、业绩、配售、停复牌、政策和每日新闻。

### 9.3 美股/美国

- S&P 500、Nasdaq 100、Dow、Russell 2000、VIX。
- 核心股票和主要行业 ETF 日行情、市场宽度。
- 2Y/10Y/30Y 美债、2s10s、美元和主要商品。
- CPI、PCE、非农、失业率、GDP、零售、ISM、初请等。
- 央行、统计、财政、监管和能源官方发布。
- 8-K、10-Q、10-K 等公司申报元数据及每日新闻。

### 9.4 Session

| session | Asia/Shanghai 建议时间 | 内容 |
|---|---:|---|
| `apac_preopen` | 08:15 | 隔夜美股、全球新闻、A/HK 开盘前 |
| `cn_hk_close` | 16:30 | A/HK 收盘和日间事件 |
| `us_close` | 按交易所收盘后约 90 分钟 | 美股收盘；必须按夏令时计算 |

调度使用交易所日历和 timezone database，不得硬编码 UTC 偏移。

---

## 10. 新闻处理规范

### 10.1 来源等级

```text
official          官方、监管、交易所、公司原始公告
licensed_media    获授权通讯社和主流财经媒体
research          研究报告和行业媒体
social            社交平台和论坛
other             未归类来源
```

低等级来源可以补充关注度，不能覆盖官方事实。冲突内容分别保留并设置 `source_conflict`。

### 10.2 两层去重

```text
来源级幂等：provider_id + provider_record_id + version
跨来源聚类：URL/内容 hash + 标题 + 实体 + 时间窗口
```

- 去 tracking 参数后得到 canonical URL。
- 标题匹配使用 Unicode NFKC、空白和普通标点规范化。
- 简繁转换只用于匹配，不覆盖原文。
- “未下调”“不构成违约”等否定词不能被删除。
- 相同稿件保留各自来源记录，只共享 cluster ID。
- 相同标题但主体或关键数字不同不得当作精确重复。
- 新闻更正、撤回必须保存版本链。

### 10.3 实体与主题

首批主题枚举：

```text
monetary_policy, fiscal_policy, inflation, growth, employment,
liquidity, rates, fx, commodities, property, consumption,
technology, semiconductors, energy, geopolitics, regulation,
earnings, corporate_action, credit_risk, market_structure
```

新增主题必须修改公共 taxonomy、版本号、fixture 和契约测试。

### 10.4 厂商标注

- 原样保存 provider、annotation type、label、score、scale 和 model version。
- 不同供应商的标签不能直接平均。
- positive 不自动等于 bullish。
- 未打标必须为 null，禁止补 neutral。
- 历史分析前必须确认标签是当时生成还是后来重算。

### 10.5 内部 LLM 输入

内部个人工作流可以使用 EditorContext 中的全部规范化字段。新闻是否包含正文只由请求的
`content_mode` 和仓库实际数据决定，不按旧 rights 标记二次过滤。API key、Authorization、
Cookie、账号、密码和其他凭据始终禁止进入输入。

---

## 11. 测试总则

### 11.1 分层

```text
L1  unit                    纯函数、无数据库、无网络
L2  provider contract       所有 Provider 共享同一测试套件
L3  fixture/parser          模拟上游完整响应
L4  integration             真实临时 PostgreSQL
L5  API                     FastAPI + 数据库
L6  e2e                     worker → DB → API
L7  live/nightly            受控真实数据源
L8  quality/soak            数据质量和连续运行
```

PR 默认禁止访问公网。当前时间、随机数、UUID、退避等待、HTTP 和交易日历版本都必须可注入或冻结。

每个 provider 至少准备：

```text
success.json
empty.json
missing_fields.json
auth_failure.json
rate_limited.json
timeout.json
schema_changed.json
duplicate_page.json
```

fixture 使用合成或脱敏数据，不包含 token、Cookie 或个人信息。

### 11.2 测试命令

```bash
uv sync --dev
uv run ruff format --check .
uv run ruff check .
uv run mypy --strict src
uv run pytest -m "not live" --cov=macro_platform --cov-report=term-missing
```

真实数据库：

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run pytest tests/integration tests/e2e -m "not live" -q
```

在线冒烟：

```bash
uv run pytest tests/live -m live -q
```

---

## 12. 明确测试用例

下列表格是最低必测集合。测试 ID 必须进入实际 pytest 名称或 marker，不能只存在于文档。

### 12.1 Symbol

| ID | 输入 | 期望输出 |
|---|---|---|
| `SYM-001` | `sh600519`, exchange=SSE | `canonical_symbol=XSHG:600519`，保留 source symbol |
| `SYM-002` | `000001.SZ` | `XSHE:000001` |
| `SYM-003` | `700.HK` | `XHKG:00700`，保留五位前导零 |
| `SYM-004` | `aapl`, exchange=NASDAQ | `XNAS:AAPL` |
| `SYM-005` | `BRK.B`, exchange=NYSE | `XNYS:BRK.B`，不得按点错误拆分 |
| `SYM-006` | 相同数字代码、两个 exchange | 得到两个不同 MIC，不凭数字猜市场 |
| `SYM-007` | `UNKNOWN`, exchange=null | quarantine，`SYMBOL_UNRESOLVED` |
| `SYM-008` | 同一公司旧/新代码及有效日期 | 同一 instrument ID，alias 有效期不同 |
| `SYM-009` | 同日期一个 alias 指向两个标的 | 拒绝加载，`AMBIGUOUS_SYMBOL_ALIAS` |
| `SYM-010` | 已是 `XHKG:00700` | normalize 幂等 |
| `SYM-011` | 空 source symbol | 当前记录隔离，同批合法记录继续 |
| `SYM-012` | 退市后查询旧 alias | 默认不解析；include_inactive 时返回 inactive |

### 12.2 时间与交易日

| ID | 输入 | 期望输出 |
|---|---|---|
| `TIME-001` | `2026-07-23T09:30:00+08:00` | `2026-07-23T01:30:00Z` |
| `TIME-002` | 无时区 `2026-07-23 09:30` | 拒绝，`TIMEZONE_REQUIRED` |
| `TIME-003` | start/end 各有一条记录 | 返回 start，不返回 end |
| `TIME-004` | HK 本地 09:30 | trading_date 按 Hong Kong 计算 |
| `TIME-005` | 美东夏令时 09:30 | 正确转换 UTC，不使用固定偏移 |
| `TIME-006` | DST 不存在的本地时间 | `NONEXISTENT_LOCAL_TIME` |
| `TIME-007` | 只有日期 `2026-07-23` | `time_precision=date`，不伪造发布时间 |
| `TIME-008` | published 09:00, available 09:03, ingested 09:04 | 校验通过 |
| `TIME-009` | available 晚于 ingested | quarantine，`INVALID_TIME_ORDER` |
| `TIME-010` | 亚洲行情 UTC 落在前一日 | trading_date 仍按当地市场正确 |
| `TIME-011` | 休市日查询日线 | 空 items + `calendar_status=closed` |
| `TIME-012` | start=end | 422 `INVALID_RANGE` |
| `TIME-013` | 相同 calendar version 重放 | 结果确定且返回 calendar version |

### 12.3 数值与单位

| ID | 输入 | 期望输出 |
|---|---|---|
| `UNIT-001` | `5.2%` | `value="5.2", unit="percent"` |
| `UNIT-002` | `25bp` | `value="0.25", unit="percent"` |
| `UNIT-003` | `1.2亿元 CNY` | `value="120000000", currency="CNY"` |
| `UNIT-004` | `12.5`, scale=1000 USD | `value="12500"` |
| `UNIT-005` | 指数 3123.45 | unit=index_point，不标货币 |
| `UNIT-006` | CPI 有值无单位 | quarantine，`UNIT_REQUIRED` |
| `UNIT-007` | `0.100000000000000001` | 写入读出完全相同 |
| `UNIT-008` | GDP level 与 GDP YoY | 两个不同 series/measure，不覆盖 |
| `UNIT-009` | `--`, `N/A` | value=null + missing reason，不转为 0 |
| `UNIT-010` | `-0.3%` MoM | 正确保留负数 |
| `UNIT-011` | 转换公式分母为 0 | `NORMALIZATION_ERROR` |
| `UNIT-012` | 随机 Decimal 往返 | 序列化前后严格相等 |

### 12.4 Provider Contract

| ID | 前置/输入 | 期望输出 |
|---|---|---|
| `PRV-001` | 合法行情 fixture | 严格解析为 MarketBar，source/checksum 完整 |
| `PRV-002` | 同 fixture 执行两次 | canonical 结果和 ID 相同 |
| `PRV-003` | 两页数据 | 翻页无重复、无遗漏，末页 cursor=null |
| `PRV-004` | 请求 `[09:00,10:00)`，上游含 10:00 | adapter 过滤 10:00 |
| `PRV-005` | 上游乱序 | 按公共排序稳定输出 |
| `PRV-006` | 三条中一条非法 | 两条 items；一条 quarantine；warning=1 |
| `PRV-007` | HTTP 429 + Retry-After | `ProviderRateLimitError`, retryable=true |
| `PRV-008` | HTTP 401/403 | Auth/Authorization Error，禁止无限重试 |
| `PRV-009` | 必填字段改名 | `ProviderSchemaError` + schema drift 告警 |
| `PRV-010` | 超时 | `ProviderTimeoutError`, retryable=true |
| `PRV-011` | provider 不支持历史 PIT | capability=false；`UnsupportedCapabilityError` |
| `PRV-012` | 本地时区原始记录 | 输出 UTC，审计层保留原时区/原值 |
| `PRV-013` | 调用 fetch 后检查 query | query 不得被修改 |
| `PRV-014` | 入库成功但响应丢失，重试该页 | 不产生重复业务记录 |
| `PRV-015` | 空页却持续 next_cursor | 超过阈值报 `INVALID_PAGINATION`，不死循环 |
| `PRV-016` | cursor 过期 | `ProviderCursorError`，从已提交 watermark 恢复 |
| `PRV-017` | JSON 字段顺序不同但语义相同 | canonical checksum 相同 |
| `PRV-018` | 未配置凭据 | health=not_configured，日志无 secret |
| `PRV-019` | 200 返回 HTML 登录页 / auth wall / risk-control page | `ProviderAuthorizationError`，不能当空数据 |
| `PRV-020` | 返回未知额外字段 | extra=forbid，契约测试失败 |
| `PRV-021` | malformed JSON / unexpected non-JSON provider payload | `ProviderSchemaError`，不能当空数据 |

### 12.5 新闻去重与修订

| ID | 输入 | 期望输出 |
|---|---|---|
| `NEWS-001` | 相同 provider/source ID/version 写两次 | 仅一条来源记录 |
| `NEWS-002` | URL 只差 utm 参数 | canonical URL 相同 |
| `NEWS-003` | 全半角、空格、普通标点不同的标题 | fingerprint 相同 |
| `NEWS-004` | 两家媒体转载同稿 | 两条 news，共享 cluster ID |
| `NEWS-005` | 相同标题、不同公司 | 不同 cluster |
| `NEWS-006` | “增长10%”与“增长15%” | 不作为精确重复 |
| `NEWS-007` | “将收购”与“否认收购” | 不合并为同一语义版本 |
| `NEWS-008` | 相同 source ID，version+1 | 两个版本，旧版不覆盖 |
| `NEWS-009` | retracted | 原记录保留，状态变更有生效时间 |
| `NEWS-010` | 次日再次发布相同内容 | 超出聚类窗口后保留新发行记录 |
| `NEWS-011` | T2 才增加权威来源 | T1 as_of 看不到 T2 来源 |
| `NEWS-012` | 只有标题无正文 | 接受，content_mode=headline |
| `NEWS-013` | 厂商情绪为空 | vendor annotation 为空，不补 neutral |
| `NEWS-014` | 两供应商相反情绪 | 分别保留，不生成平台统一标签 |
| `NEWS-015` | “未下调”“不构成违约” | 规范化后仍保留否定词 |
| `NEWS-016` | source ID 缺失、有稳定 URL/hash | 使用明确 identity_basis，不随机 ID |
| `NEWS-017` | legacy external_llm_allowed=false | EditorContext 保留原始 summary/body |
| `NEWS-018` | 500 对人工 golden 新闻 | 聚类 precision/recall 达第 14 节阈值 |

### 12.6 Point-in-time：发布阻断项

| ID | 输入 | 期望输出 |
|---|---|---|
| `PIT-001` | 初值100于10日发布，修订98于20日；as_of=15日 | 返回100 |
| `PIT-002` | 同上，as_of=21日 | 返回98，可查看修订链 |
| `PIT-003` | 新闻发布09:00，first seen 09:07；as_of=09:05 | 不返回 |
| `PIT-004` | 同上，as_of=09:07 | 返回，basis=first_seen |
| `PIT-005` | 可信历史分发时间09:00，次日才回补 | as_of=09:01 可返回，basis=provider_disseminated |
| `PIT-006` | 先查当前再查旧 as_of | 旧查询仍返回旧 vintage |
| `PIT-007` | 标的 T2 才纳入指数 | T1 标的池不含该标的 |
| `PIT-008` | T2 才补实体/摘要 | T1 上下文不能看到 T2 增强字段 |
| `PIT-009` | context fixture 混有未来记录 | 所有响应 available_at <= as_of |
| `PIT-010` | 同参数、不同 as_of | 缓存键不同 |
| `PIT-011` | 冻结数据/日历/as_of 后生成两次 | 业务内容和 fingerprint 相同 |
| `PIT-012` | 宏观事件已排期但未发布 | 仅在 upcoming releases，无 actual |
| `PIT-013` | 新闻后续撤回，查询撤回前 | 返回当时可见原文，不标已撤回 |
| `PIT-014` | 查询撤回后 | 按接口策略返回撤回状态并保留审计链 |
| `PIT-015` | 五年前回填、无可信 available_at | 标记非 PIT 或隔离，不把文章日期冒充 |
| `PIT-016` | 10,000 条混合全库回放 | 未来泄漏数=0 |
| `PIT-017` | 派生指标某输入晚于 as_of | 不生成；max_input_available_at 必须 <= as_of |

派生数据必须额外保存：

```text
calculated_at
max_input_available_at
calculation_version
input_record_ids
```

### 12.7 数据库

| ID | 输入/故障 | 期望输出 |
|---|---|---|
| `DB-001` | 空库执行全部 migration | 成功且 schema version 正确 |
| `DB-002` | 上一发布版本升级 | 数据无损，约束/索引存在 |
| `DB-003` | migration 中途失败 | 事务回滚，无半升级 |
| `DB-004` | 两 worker 并发写同一记录 | 唯一约束保证一条 |
| `DB-005` | 两 worker 更新 watermark | 不丢数据、不倒退 |
| `DB-006` | 批次第51/100条非法 | 按明确事务策略隔离/回滚，状态确定 |
| `DB-007` | 数据提交后 checkpoint 失败 | 重放安全且无重复 |
| `DB-008` | 高精度 Decimal | 读写完全相同 |
| `DB-009` | 非 UTC offset timestamp | 读出为同一 UTC 瞬间 |
| `DB-010` | 两个 macro vintage | 两版本都存在且链完整 |
| `DB-011` | 删除被事实表引用 instrument | 外键阻止硬删除 |
| `DB-012` | 原始 payload/hash | 能追溯 provider/source/run |
| `DB-013` | 非法记录写 quarantine | 有错误码、脱敏片段、run ID，可重放 |
| `DB-014` | 翻页期间插入新记录 | cursor snapshot 保证无重无漏 |
| `DB-015` | 备份恢复 fixture DB | 行数、checksum、版本链一致 |
| `DB-016` | 核心查询 EXPLAIN | 使用预期索引，无无界全表扫描 |
| `DB-017` | retention 删除 raw | canonical/审计按策略保留，删除可审计 |

### 12.8 REST API

| ID | 请求 | 期望输出 |
|---|---|---|
| `API-001` | 合法 market bars | 200，符合 OpenAPI，Decimal 为字符串 |
| `API-002` | start/end 边界记录 | 仅返回 `[start,end)` |
| `API-003` | start>=end | 422 `INVALID_RANGE` |
| `API-004` | 未知 instrument ID | 列表 200 items=[]；解析接口 404 |
| `API-005` | 三页数据 | 无重复、无遗漏、排序稳定 |
| `API-006` | 篡改 cursor | 400 `INVALID_CURSOR` |
| `API-007` | limit=1000000 | 422，不默默拉全表 |
| `API-008` | 无 token | 401 `UNAUTHENTICATED` |
| `API-009` | 无数据集权限 | 403 `FORBIDDEN` |
| `API-010` | 达到 API 配额 | 429 + Retry-After |
| `API-011` | `X-Request-ID=req_test` | 响应和日志携带同 ID |
| `API-012` | 相同参数请求两次 | 排序和业务内容一致 |
| `API-013` | as_of + 未来 fixture | 未来数据全部排除 |
| `API-014` | 部分数据源不可用 | 200/206，coverage=partial/degraded |
| `API-015` | 必需数据全部不可用且 fail=true | 503 `DATASET_UNAVAILABLE` |
| `API-016` | 合法新闻查询无记录 | 200 items=[]，不是404 |
| `API-017` | 输入 `+08:00` | 正确转换，响应统一 Z |
| `API-018` | 请求未知字段 | 422，不能静默忽略 |
| `API-019` | `/health/live` | 只检查进程，200 |
| `API-020` | DB down 时 `/health/ready` | 503 |
| `API-021` | OpenAPI snapshot | 无未经批准破坏性变化 |
| `API-022` | 含凭据字段的输入 | 不通过嵌套字段泄漏凭据 |
| `API-023` | 固定 EditorContext 请求 | 四类数据、coverage、PIT 均正确 |
| `API-024` | SQL/控制字符参数 | 参数化安全，无 DB 异常/注入 |

### 12.9 故障、降级和恢复

| ID | 故障 | 期望输出 |
|---|---|---|
| `DEG-001` | 连续超时，最多3次 | fake clock 验证退避，调用正好3次 |
| `DEG-002` | 429 Retry-After=10 | 10秒前不重试 |
| `DEG-003` | 60秒内连续5次失败 | circuit open，后续快速失败 |
| `DEG-004` | 冷却后探测成功 | half-open → closed |
| `DEG-005` | 主源失败、备用正常 | provenance 显示备用，不静默混合 |
| `DEG-006` | 主源失败、只有旧数据、allow_stale=true | degraded + stale + age |
| `DEG-007` | allow_stale=false | 503，不偷返旧数据 |
| `DEG-008` | 新闻失败、行情正常 | context 部分成功，新闻 unavailable |
| `DEG-009` | DB 写失败 | 不推进 watermark、不标成功 |
| `DEG-010` | 一条 poison record | 隔离并告警，其余按策略处理 |
| `DEG-011` | worker 中途终止 | 重启从 checkpoint 恢复，无重复 |
| `DEG-012` | 必填字段 schema drift | 阻断该 adapter，不批量写 null |
| `DEG-013` | debug 日志下鉴权失败 | token/cookie/账号全部脱敏 |
| `DEG-014` | 系统时钟偏移超阈值 | readiness degraded 并告警 |
| `DEG-015` | provider 恢复 | 自动补缺口，watermark 连续 |
| `DEG-016` | 两来源数值冲突 | 保留双方和 conflict，不无规则覆盖 |

### 12.10 E2E 与在线

| ID | 输入 | 期望输出 |
|---|---|---|
| `E2E-001` | 空库 + Mock CN/HK | instrument、行情、宏观、新闻入库 |
| `E2E-002` | 空库 + Mock US | 使用相同公共模型，无区域私有 API 字段 |
| `E2E-003` | 完成抓取后请求 context | 数据、来源、freshness、quality 完整 |
| `E2E-004` | 同任务执行两次 | 表行数和业务 checksum 不变 |
| `E2E-005` | 两个历史 as_of | 返回对应 vintage |
| `E2E-006` | 一个 provider 失败 | 成功数据输出，失败 coverage=degraded |
| `E2E-007` | 新闻转载、更正、撤回 | 来源/cluster/版本均可追溯 |
| `E2E-008` | 任务中崩溃后重启 | 最终 checksum 等于一次成功运行 |
| `LIVE-001` | 测试凭据 health | 鉴权成功，延迟在阈值内 |
| `LIVE-002` | 每市场一个活跃标的日线 | OHLC、代码、时间、单位合法 |
| `LIVE-003` | 小范围分页 | cursor 可用，无重复 source ID |
| `LIVE-004` | 区域新闻短窗口 | 全在范围内；0条允许但有明确状态 |
| `LIVE-005` | 宏观序列 | 日期单调、单位和 revision 可解析 |
| `LIVE-006` | 两合法来源对账 | 同口径误差超阈值时生成 reconciliation |
| `LIVE-007` | schema snapshot 对比 | 删除/改名/类型变化阻断 adapter |
| `LIVE-008` | 运行日志扫描 | 无 token、Cookie、账号或密码 |

在线测试不得断言“当前价格等于固定值”或“新闻数量一定大于零”，只能断言契约、不变量、范围和合理性。

---

## 13. CI 分层

### 每次提交（目标 ≤ 10 分钟）

- ruff format/check。
- mypy strict。
- unit。
- provider contract。
- OpenAPI compatibility。

### 每个 PR

- 临时 PostgreSQL integration。
- migration fresh/upgrade。
- PIT 回放。
- API 与 Mock provider E2E。
- secret、依赖漏洞和依赖许可证扫描。
- Docker 镜像构建。

### Nightly

- 在线 provider smoke。
- 跨来源行情对账。
- schema drift。
- gap detection 和质量报告。
- 性能基准。

### Release

- 全量 E2E。
- 历史 as_of 回放。
- 故障注入。
- 备份恢复抽查。
- API 向后兼容和数据授权字段检查。

覆盖率门槛：

```text
symbol/time/unit/PIT 核心逻辑  branch >= 95%
公共 contract/storage/API      branch >= 90%
Provider                       branch >= 80%
全项目                         branch >= 85%
```

覆盖率不能替代真实数据库、契约和 PIT 测试。

---

## 14. 数据质量发布门槛

| ID | 指标 | 门槛 |
|---|---|---:|
| `QA-001` | 公共必填字段完整率 | 100% |
| `QA-002` | 声明范围内 symbol 解析率 | ≥ 99.95% |
| `QA-003` | 来源级精确重复率 | 0 |
| `QA-004` | 新闻 golden 聚类 precision | ≥ 95% |
| `QA-005` | 新闻 golden 聚类 recall | ≥ 90% |
| `QA-006` | PIT 未来泄漏 | 0 / 10,000 |
| `QA-007` | Decimal 往返误差 | 0 |
| `QA-008` | 核心日线交易日覆盖率 | ≥ 99.5% |
| `QA-009` | EOD 收盘后90分钟内完成 | ≥ 99% |
| `QA-010` | 授权新闻延迟 | p95 ≤ 10分钟或不差于合同 SLA |
| `QA-011` | 定时宏观发布延迟 | p95 ≤ 10分钟 |
| `QA-012` | provider 恢复后补缺口 | ≤ 24小时 |
| `QA-013` | API 月可用性 | ≥ 99.5% |
| `QA-014` | 普通列表 API | p95 ≤ 300ms，p99 ≤ 1s |
| `QA-015` | EditorContext，不含 LLM | p95 ≤ 2s |
| `QA-016` | 同口径跨来源价格差 | ≤ 1bp；超出进入冲突表 |
| `QA-017` | lineage 抽样可追溯率 | 100% |
| `QA-018` | 关键数据 freshness 字段 | 100% |
| `QA-019` | 密钥泄漏 | 0 |
| `QA-020` | 未版本化破坏性 API 变化 | 0 |
| `QA-021` | fixture 重放三次 checksum | 完全一致 |

质量报告按 `provider × region × dataset × trading_date` 输出：

```text
record_count, expected_count, coverage_rate,
required_field_null_rate, unresolved_symbol_count,
duplicate_count, quarantine_count,
freshness_p50, freshness_p95, revision_count,
schema_drift_status, last_success_at
```

情绪字段不设强制非空率。缺失必须保持 null。

---

## 15. 授权、安全与可观测性

### 15.1 数据源登记

每个来源建立 `docs/data-sources/<source>.md`：

```text
数据所有者：
官方文档/合同：
接入方式：
账号负责人：
允许服务器运行：
历史存储说明：
正文范围：
保留期限说明：
内部使用备注：
要求署名：
速率与并发限制：
授权到期日：
```

这些登记项用于来源研究和未来部署评估，不参与当前内部个人运行时准入。

### 15.2 Secret

- 仓库只提交 `.env.example`，值为空或无效占位符。
- 本地使用未跟踪 `.env`；CI/生产使用 Secret Manager。
- 每个 provider 使用独立最小权限账号。
- 日志脱敏 Authorization、Cookie、token、账号和受限正文。
- 发现泄漏必须立即吊销和轮换，仅从 Git 删除不算完成。
- CI 使用 gitleaks 或同类 secret scanner。

### 15.3 结构化日志

每条日志至少包含：

```text
timestamp, level, service, request_id/run_id,
provider_role, dataset, region, action,
duration_ms, record_count, error_code
```

禁止日志：原始 token、Cookie、整篇新闻正文、大型 raw payload。

### 15.4 指标

```text
provider_request_total
provider_request_error_total{code}
provider_request_duration_ms
provider_records_fetched_total
provider_records_rejected_total
provider_last_success_at
provider_data_latest_available_at
provider_stale_seconds
news_duplicate_ratio
news_missing_published_at_ratio
schema_validation_failure_total
fallback_activation_total
context_build_duration_ms
```

Provider 状态：

```text
ok              主来源正常、数据未过期
degraded        fallback、部分失败或软过期
down            无可用数据
not_configured  未配置或未采购
```

---

## 16. 人员分工和目录所有权

### 16.1 实习生 A：A股 + 港股

负责：

- CN/HK 数据源清单、字段字典、频率、历史深度、限流与授权。
- A股、港股证券主数据、alias、交易日历、币种和时区。
- A/H 行情、市场指标、资金流和区域特有清洗。
- 中国/香港宏观、政策、公告和每日新闻 Provider。
- CN/HK 新闻实体映射、主题规则、去重 fixture。
- 区域 unit、contract、live、quality tests。
- CN/HK 数据源运行手册和质量日报。

拥有目录：

```text
src/macro_platform/providers/cn/
src/macro_platform/providers/hk/
src/macro_platform/normalization/cn_hk/
tests/fixtures/cn/
tests/fixtures/hk/
tests/**/cn/
tests/**/hk/
docs/data-sources/cn-*.md
docs/data-sources/hk-*.md
```

不得：

- 单方面修改公共 contract、API 或数据库语义。
- 修改 US provider 业务规则。
- 生成宏观观点和交易建议。

### 16.2 实习生 B：美股 + 公共底座

负责：

- US 数据源清单、字段字典、频率、历史深度、限流与授权。
- 美股证券主数据、alias、交易日历、夏令时和半日市。
- 美股行情、市场宽度、利率、美元、商品、美国宏观和每日新闻。
- 公司监管申报元数据。
- 公共 Pydantic contracts、Provider Protocol、错误模型和 registry。
- PostgreSQL、SQLAlchemy、Alembic、repository 与幂等写入。
- FastAPI、认证、分页、EditorContext、worker、观测和 CI。
- US fixture、测试和运行手册。

拥有目录：

```text
src/macro_platform/providers/us/
src/macro_platform/normalization/us/
src/macro_platform/contracts/
src/macro_platform/storage/
src/macro_platform/api/
src/macro_platform/jobs/
src/macro_platform/observability/
migrations/
.github/
tests/fixtures/us/
```

B 是公共组件维护者，不是公共契约的单方面决定者。公共模型、时间语义、数据库语义和 API 破坏性变化必须由 A 和项目负责人共同批准。

### 16.3 项目负责人

负责：

- 冻结和批准 v1 contract。
- 宏观总编 LLM、上下文 token 预算和消费端验收。
- 数据供应商采购、授权、账号与密钥所有权。
- 审批 ADR、破坏性 migration 和 API 版本变化。
- 决定哪些数据缺口允许生成报告，哪些必须阻断。
- 最终发布和质量验收。

不负责替实习生修 adapter 或手工清洗日常数据。

### 16.4 交叉评审

- A 评审 B 的 US 新闻是否符合统一 NewsEvent。
- B 运行公共 contract suite 验收 A 的 CN/HK providers。
- 作者不能批准自己的 PR。
- 公共 contract/API/migration 必须 A、B 交叉评审并由负责人批准。
- 来源冲突保留 provenance，不能凭个人感觉选值。

### 16.5 工作量平衡

CN/HK 来源和口径较复杂，因此 A 不承担数据库和公共 API。US 官方接口通常较规范，因此 B 同时承担公共基础设施。A 完成区域 M2 后可以协助公共测试，但不跨区域改变数据语义。

---

## 17. CODEOWNERS 与分支保护

建议 `.github/CODEOWNERS`：

```text
*                                                   @project-lead

/src/macro_platform/providers/cn/                    @intern-a @project-lead
/src/macro_platform/providers/hk/                    @intern-a @project-lead
/src/macro_platform/normalization/cn_hk/             @intern-a @project-lead
/tests/fixtures/cn/                                  @intern-a
/tests/fixtures/hk/                                  @intern-a

/src/macro_platform/providers/us/                    @intern-b @project-lead
/src/macro_platform/normalization/us/                @intern-b @project-lead
/tests/fixtures/us/                                  @intern-b

/src/macro_platform/contracts/                       @intern-a @intern-b @project-lead
/src/macro_platform/storage/                         @intern-b @project-lead
/src/macro_platform/api/                             @intern-b @project-lead
/src/macro_platform/jobs/                            @intern-b @project-lead
/src/macro_platform/observability/                   @intern-b @project-lead
/migrations/                                         @intern-b @project-lead
/.github/                                            @intern-b @project-lead
/docs/adr/                                           @project-lead
```

CODEOWNERS 多 owner 不代表全部必须批准，因此分支保护额外规定：

- 禁止直接 push `main`。
- 普通区域 PR 至少一名非作者批准。
- contracts、API、migration、安全、授权 PR 至少两名批准，且包括负责人。
- 所有 review conversation 必须 resolved。
- 必须通过全部 required CI checks。
- 禁止 force push `main`。
- squash merge，合并后自动删除分支。

---

## 18. Issue、分支、Commit 和 PR

### 18.1 Issue 先行

任何代码前必须有 Issue，包含：

```text
Owner：
涉及目录：
数据源及官方文档：
入参：
出参：
错误和降级行为：
验收测试 ID：
明确不在范围内：
公共契约/数据库/授权影响：
依赖：
预计完成时间：
```

### 18.2 短分支

```text
feat/cn-market-bars
feat/hk-daily-news
feat/us-macro-releases
fix/hk-timezone-normalization
test/provider-pagination-contract
chore/ci-migration-check
```

- 一个分支只解决一个 Issue。
- 理想生命周期 ≤ 2 个工作日，最长 3 天。
- 大功能拆成可独立合并的小 PR。
- 每天从 main rebase，禁止长期 develop 分支。
- 未完成能力用配置开关，不保留长期 feature branch。

### 18.3 Conventional Commits

```text
feat(cn): add daily bar provider
feat(us): add filing metadata ingestion
feat(core): define provider page contract
fix(hk): preserve leading zeros in symbols
test(api): cover invalid cursor
docs(adr): record point-in-time policy
```

每个 commit 一个逻辑变化。PR 使用 squash merge，PR 标题成为最终 commit。

### 18.4 PR 大小与模板

PR 建议 ≤ 400 行逻辑代码；fixture、锁文件和生成文件可排除。超过 500 行必须解释为什么不能再拆。

```text
关联 Issue：
修改目的：
涉及目录：
输入示例：
输出示例：
错误与降级行为：
公共 contract 是否变化：
数据库是否变化：
内部数据使用边界是否变化：
新增测试及测试 ID：
本地测试结果：
在线验证方式：
回滚方式：
```

评审重点是正确性、契约兼容性、时间语义、幂等、授权和可运维性，不评论可由 formatter 自动解决的琐碎格式。

### 18.5 Bug 规则

- 每个 bug 先新增一个能稳定复现的失败测试。
- 修复后测试转绿，再提交实现。
- 不允许用 skip/xpass 永久绕过；临时隔离必须关联 Issue、owner 和到期日期。

---

## 19. Contract、API 与数据库变更

### 19.1 Contract

- `contracts/` 和生成的 OpenAPI 是唯一事实来源。
- v1 新增可选字段属于兼容变化。
- 删除、改名、改变类型、单位或语义属于破坏性变化。
- 破坏性变化必须 ADR + 新 API 版本或正式弃用周期。
- Provider 特例封装在 adapter，不污染公共模型。
- 每次 contract 变化同步更新示例、OpenAPI snapshot 和消费者测试。

### 19.2 Migration

- 每个 schema 变化一个 Alembic migration。
- 已进入 main 或共享环境的 migration 永远不修改，只新增后续 migration。
- CI 保证只有一个 migration head。
- 必须测试空库升级和上一发布版本升级。
- schema migration 与大规模 backfill 分开。
- 删除/改类型采用 expand–migrate–contract：

```text
1. 新增兼容字段/表
2. 发布双读/双写兼容代码
3. 独立任务回填历史数据
4. 切换读写并观察
5. 后续版本删除旧结构
```

不可逆、大表锁定或删除数据的 migration 必须有备份、验证和回滚方案，并由负责人批准。

### 19.3 ADR

以下情况必须写 `docs/adr/NNNN-title.md`：

- 引入或替换关键数据源。
- 修改公共 schema、时间或单位语义。
- 修改去重主键、revision 或 PIT 策略。
- 引入缓存、队列或新存储。
- 改变重试、checkpoint 和 backfill 策略。
- API 版本变化。
- 改变内部数据使用边界或凭据隔离规则。

模板：

```text
状态：proposed/accepted/superseded
背景：
决定：
备选方案：
选择理由：
影响：
上线方案：
回滚方案：
```

---

## 20. 四周里程碑

### 第 1 周：契约和并行骨架

项目负责人：

- 冻结本文件中的 v1 输入输出。
- 明确首批指标、主题和数据源授权。

实习生 B：

- 初始化仓库、CI、Pydantic contracts、Provider Protocol。
- 建 PostgreSQL/Alembic/API 空壳和 fake repository。

实习生 A：

- 建 CN/HK provider、normalization、fixture 骨架。
- 完成代码、交易日历、时区和单位 mapping 测试。

验收：A股、港股、美股各一个 fake provider 通过相同 contract suite；A 不需要等待真实 API/DB 即可开发。

### 第 2 周：区域数据闭环

实习生 A：

- 完成 CN/HK 核心行情、宏观、公告和每日新闻。

实习生 B：

- 完成 US 核心行情、宏观、申报和每日新闻。
- 完成幂等入库、分页、checkpoint、重试和限流。

验收：所有 provider 可完全用 fixture 离线运行；可生成不含完整新闻的三地区数据包。

### 第 3 周：统一 API 和质量

- B 完成公共查询 API、EditorContext、调度、指标和 JSON 日志。
- A 完成 CN/HK 实体映射、新闻去重和区域质量规则。
- B 完成 US 映射和公共聚类支持。
- A/B 交叉运行全部 contract、PIT 和 E2E。

验收：宏观总编只通过 `/v1/editor/context` 获取三地上下文。

### 第 4 周：稳定性与交接

- 完成 nightly live smoke、schema drift 和跨来源对账。
- 完成崩溃恢复、重复执行、断点续跑和 backfill 测试。
- 完成 Docker、备份、runbook、数据字典和授权登记。
- 连续五个交易日成功生成所有 session。

验收：质量门槛通过，负责人执行最终端到端验收。

---

## 21. Definition of Done

### 21.1 单个 Provider

只有全部满足才算完成：

- 实现公共 Protocol，没有私有返回格式。
- 输入、输出和异常写入数据源文档。
- 标识、时间、币种、单位全部标准化。
- 保存 provider record ID、checksum、关键时间和 usage rights。
- 支持分页、限流、重试、checkpoint 和幂等写入。
- 正常、空、缺字段、鉴权、429、超时、schema drift、重复页 fixture 齐全。
- 通过公共 contract tests 和真实 API 冒烟。
- 有请求数、错误数、延迟、最后成功时间和 freshness 指标。
- 无 secret 或未授权内容进入 Git。
- CI 全绿，另一名实习生完成交叉评审。

### 21.2 整个平台

- 三地数据通过统一 API 获取。
- 历史 as_of 回放未来泄漏为零。
- 重放、崩溃恢复和 backfill 幂等。
- 数据库从零 migration 和上一版本升级通过。
- EditorContext 确定、可追溯、带 coverage 和 fingerprint。
- 宏观总编无需了解 provider 字段。
- 连续五个交易日自动运行达到 SLA。
- 数据授权、安全、备份恢复和运行手册验收完成。

---

## 22. 首批 Issue 清单

### 实习生 A

1. `CN/HK data-source and rights matrix`
2. `CN/HK instrument master and effective-dated aliases`
3. `CN/HK calendar, timezone and unit fixtures`
4. `CN core index and daily bars provider`
5. `HK core index and daily bars provider`
6. `CN macro and cross-asset providers`
7. `HK flows, rates and market observations`
8. `CN/HK official news and announcement providers`
9. `CN/HK licensed news provider POC`
10. `CN/HK entity, topic and dedup golden set`
11. `CN/HK quality report and runbooks`

### 实习生 B

1. `Repository bootstrap, CI and branch protection`
2. `Pydantic contracts v1 and Provider Protocol`
3. `PostgreSQL schema and Alembic migrations`
4. `Shared Provider contract test suite`
5. `US instrument master and effective-dated aliases`
6. `US calendars, DST and half-day fixtures`
7. `US market, rates, FX and cross-asset providers`
8. `US official macro providers`
9. `US filing metadata provider`
10. `US daily news provider and dedup fixtures`
11. `FastAPI routes, auth, cursor and OpenAPI snapshot`
12. `Worker, checkpoint, retry and health metrics`
13. `EditorContextService and PIT filtering`
14. `Docker deployment, backup and shared runbook`

---

## 23. 不可妥协的十条规则

1. API 不在请求期间抓外部数据。
2. Provider 不写数据库、不生成观点。
3. 所有区域共用一套 contract。
4. Decimal 不经过 float。
5. 所有 timestamp 带时区，范围左闭右开。
6. 所有历史结果满足 `available_at <= as_of`。
7. 错误、空结果、partial 和 stale 必须可区分。
8. 上游异常不能通过返回空数组吞掉。
9. 密钥、账号、密码和 Cookie 不进入 Git、日志或 LLM。
10. 没有测试、交叉评审和 CI 的代码不得进入 main。

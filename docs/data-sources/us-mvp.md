# 数据源矩阵：US two-day MVP

## 所有权

- Owner：@kazming666
- 账号负责人：@Detachm（待正式分配；当前无 live 凭据提交）
- 采购/合同负责人：@Detachm（待数据采购/法务复核）
- 区域：US / GLOBAL
- Provider roles：
  - `us.instruments.primary`
  - `us.market.primary`
  - `us.rates_fx.primary`
  - `us.macro.primary`
  - `us.filings.primary`
  - `us.news.primary`
- 数据集：instruments / bars / market_observations / macro_series / macro_observations / macro_releases / news
- 关联 Issue：[#2](https://github.com/Detachm/macro-data-platform/issues/2)
- 首次批准日期：Twelve Data Basic 的 `SPY`、`QQQ`、`DIA` 日线于 2026-07-27 获 @Detachm 批准；其余来源仍待批准。
- 复核日期：首次批准后 30 天，之后每季度复核一次；商业合同或官方条款变化时立即复核。

本文件是 Issue #2 要求的 US 两日 MVP 聚合矩阵。具体来源仍按工程规范分拆为 source-level 登记文件：

| Source | 登记文件 |
|---|---|
| Nasdaq Trader Symbol Directory | [us-nasdaq-trader-symbol-directory.md](us-nasdaq-trader-symbol-directory.md) |
| SEC EDGAR / SEC RSS | [us-sec-edgar.md](us-sec-edgar.md) |
| Polygon/Massive | [us-polygon-massive-market-data.md](us-polygon-massive-market-data.md) |
| Alpha Vantage | [us-alpha-vantage.md](us-alpha-vantage.md) |
| Twelve Data Basic | [us-twelve-data.md](us-twelve-data.md) |
| Federal Reserve H.10/H.15/DDP | [us-federal-reserve-h10-h15.md](us-federal-reserve-h10-h15.md) |
| U.S. Treasury interest rates | [us-treasury-interest-rates.md](us-treasury-interest-rates.md) |
| BLS Public Data API | [us-bls-api.md](us-bls-api.md) |
| BEA API | [us-bea-api.md](us-bea-api.md) |
| FRED/ALFRED | [us-fred-alfred.md](us-fred-alfred.md) |
| GDELT | [us-gdelt.md](us-gdelt.md) |
| NewsAPI / licensed media placeholder | [us-newsapi.md](us-newsapi.md) |

研究附件：[us-mvp-research.md](us-mvp-research.md) 只保存调研证据和来源链接，不是冻结合同来源；若与本文件或 source-level 登记冲突，以本文件和 source-level 登记为准。

## 结论摘要

两日 MVP 的目标是证明 US 数据源抽象、字段映射、授权边界和 fixture-backed provider 纵向切片，
原则上不承诺生产 live 接入。唯一的后续例外是 ADR 0003 批准的 Twelve Data Basic 三个
US market proxy 日线范围；其 live adapter 仍由 #34 单独实现。

| 数据类 | 两日 MVP 状态 | Primary | Fallback / gap | 结论 |
|---|---|---|---|---|
| instruments | `fixture-only`，可用官方公开源设计 live adapter | Nasdaq Trader Symbol Directory + SEC company tickers/CIK enrichment | Polygon/Massive reference tickers if licensed | Nasdaq/SEC 可支持合成 fixture；交易所目录权利需复核后才能宣称 live-ready。 |
| daily bars | `live-approved`，仅内部 scope | Twelve Data Basic：`SPY`、`QQQ`、`DIA` 的 raw `1day` OHLCV | 无生产 fallback；Polygon/Massive、Alpha Vantage 仍未获批 | 仅允许内部 ingestion 与 canonical facts 存储；禁止外部 LLM、引用和再分发。详见 ADR 0003。 |
| rates / FX / cross-asset observations | `live-candidate` for official public releases; MVP 仍用 fixture | Federal Reserve H.15/H.10 Data Download/current releases；Treasury official feeds where available | FRED 只作发现/人工核对，不作为持久化 ingest 源 | 官方利率/汇率可做首批 market observations；在逐 series owner rights review 前，平台对 FRED 采用 no-ingest/no-LLM 保守策略。 |
| macro observations / releases | `live-candidate` for direct agency APIs; MVP 可 fixture | BLS Public Data API、BEA API、官方 release calendar | FRED 只作非持久化发现 | BLS/BEA 是 CPI、employment、GDP、PCE 等主源；available_at 不得早于 API/平台首次可见时间。 |
| SEC filing metadata | `live-candidate` | SEC EDGAR `data.sec.gov` submissions + index files | Commercial SEC vendors only for enhanced search | SEC JSON APIs 无 API key，但必须遵守 fair access；只采 metadata，不采完整正文作为新闻正文。 |
| daily news | `fixture-only` | Official releases/filings for `official` tier；GDELT disabled until rights approval | Licensed media provider pending procurement | 商业新闻正文/摘要默认不可外发；NewsAPI/Alpha Vantage/GDELT 等需合同或 rights approval 后才能进入 live。 |

## 通用规则

- 所有源的 adapter 输出只允许进入现有 `contracts/`：`Instrument`、`MarketBar`、`MarketObservation`、`MacroSeries`、`MacroObservation`、`MacroRelease`、`NewsEvent`。
- Provider 不写数据库；worker pipeline 完成校验、去重、quarantine 和幂等入库。
- 所有 timestamp 必须是 timezone-aware；入库和 API 统一 UTC `Z`。
- 历史输出必须满足 `available_at <= request.as_of`。
- Decimal 从原始字符串构造，不经过 `float`。
- 没有可审计授权的正文、summary、token、Cookie、账号信息不得进入 Git、日志、fixture 或 EditorContext。
- FRED API 默认不作为持久化 ingest 源：FRED terms 要求对第三方 series 遵守数据所有者的版权、许可和限制，非个人使用前取得 data owner permission。两日 MVP 采用平台 no-ingest/no-LLM/no-embedding 策略，直至逐 series rights review 与负责人批准完成；这不是把平台策略表述为 FRED 的一概 storage 或 AI 禁令。

## 公共 identity、checksum 与排序规则

| 数据集 | identity basis | 公共 ID 规则 | `source.provider_record_id` | checksum | source URL | 稳定排序键 |
|---|---|---|---|---|---|---|
| instruments | `venue_mic + local_symbol + valid_from` | `ins_us_<sha256(UTF-8(canonical_symbol + first_valid_from ISO-8601 date))[:16]>`（无分隔符；不含 issuer/CIK） | `<provider_id>:<source_symbol>:<valid_from>` | canonical source row JSON，不含 retrieved_at | Nasdaq/SEC record URL or source file URL | `canonical_symbol ASC, valid_from ASC, instrument_id ASC` |
| daily bars | `instrument_id + interval + bar_start + adjustment + provider_id` | `bar_us_<sha256(identity_basis)[:20]>` | `<provider_id>:<ticker>:<interval>:<bar_start>:<adjustment>` | canonical OHLCV JSON + source record metadata，不含 retrieved_at | vendor aggregate URL or fixture source path | `bar_end ASC, instrument_id ASC, bar_id ASC` |
| rates/FX observations | `region + scope_type + scope_id + metric_code + observed_at + provider_id` | `obs_us_<sha256(identity_basis)[:20]>` | `<provider_id>:<metric_code>:<observed_at-or-period>` | canonical observation JSON，不含 retrieved_at | official release/download URL | `observed_at ASC, observation_id ASC` |
| macro series | `series_id` | stable `macro:US:<AUTHORITY>:<CODE>` | `<provider_id>:<authority-code>` | canonical series metadata JSON | agency series/table URL | `series_id ASC` |
| macro observations | `series_id + period_end + vintage_id + provider_id` | `mobs_us_<sha256(identity_basis)[:20]>` | `<provider_id>:<series_id>:<period_end>:<vintage_id>` | canonical observation JSON，不含 retrieved_at | agency API/release URL | `period_end ASC, series_id ASC, available_at ASC` |
| macro releases | `series_id + scheduled_at + period_end + provider_id` | `mrel_us_<sha256(identity_basis)[:20]>` | `<provider_id>:<series_id>:<scheduled_at>:<period_end>` | canonical release JSON，不含 retrieved_at | agency release calendar URL | `scheduled_at ASC, release_id ASC` |
| SEC filing metadata as news | `accessionNumber + form + cik` | `news_us_sec_<normalized_accession_number>` | `<accessionNumber>` | canonical SEC filing metadata JSON，不含 retrieved_at | SEC archive filing URL | `published_at DESC, news_id DESC` |
| daily news | source stable ID if provided; else canonical URL/content hash + published_at | `news_us_<sha256(identity_basis)[:20]>` | provider source ID, canonical URL hash, or `identity_basis:<hash>` | canonical headline/snippet metadata JSON；不含 restricted body | canonical URL / official release URL | `published_at DESC, news_id DESC` |

以上 ID 规则用于两日 fixture-backed vertical slice。若后续生产要求改 ID 语义，必须走 ADR/migration，不能原位重写历史事实。

## 数据源矩阵

### 1. Instruments：US listed instruments

| 项 | 设计 |
|---|---|
| Provider role | `us.instruments.primary` |
| Primary | Nasdaq Trader Symbol Directory：`nasdaqlisted.txt`、`otherlisted.txt`、Adds/Deletes。 |
| Enrichment | SEC company tickers / CIK mapping：`https://www.sec.gov/files/company_tickers.json`，必要时追加 `company_tickers_exchange.json` 人工验证。 |
| Fallback | Polygon/Massive reference tickers，只有在商业授权确认后启用。 |
| 官方文档 | Nasdaq Trader Symbol Directory definitions；SEC EDGAR APIs / company ticker files。 |
| Base URL / endpoint | `https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt`；`https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt`；`https://www.sec.gov/files/company_tickers.json`。 |
| 认证 | Nasdaq/SEC public endpoint 无 API key；SEC 请求必须设置可识别 User-Agent。 |
| 分页 | 文本/JSON 全量文件，无 cursor；worker 以文件 checksum 和 retrieved_at 做增量判断。 |
| 时区 | `America/New_York`；交易日按 NYSE/Nasdaq 交易日历计算，不能用固定 UTC offset。 |
| 更新 | Nasdaq Trader 文件日内周期性更新；SEC company tickers 随 SEC 文件更新。 |
| 历史深度 | 当前文件为当前/近实时目录；有效期历史需要 Adds/Deletes 和平台自己的 alias history 生成。 |
| `available_at` basis | `first_seen`，除非源文件提供可验证发布时间。 |
| MVP 状态 | `fixture-only`：先用脱敏/合成 fixture 固定 symbol、MIC、CIK、ETF、round lot、active flag。 |

公共合同映射：

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| Nasdaq `Symbol` / SEC `ticker` | `Instrument.local_symbol` | 大写；保留点号，如 `BRK.B`；不按数字猜市场。 | 是 | quarantine `SYMBOL_UNRESOLVED` |
| Nasdaq listing exchange / market category | `venue_mic` | Nasdaq → `XNAS`；NYSE/NYSE American/Nasdaq/Cboe 等必须显式映射。 | 是 | quarantine `AMBIGUOUS_SYMBOL_ALIAS` |
| `venue_mic + local_symbol` | `canonical_symbol` | `<MIC>:<LOCAL_SYMBOL>`，如 `XNAS:AAPL`、`XNYS:BRK.B`。 | 是 | quarantine |
| SEC `cik_str` | alias metadata / issuer enrichment | 不进入 `instrument_id` seed；canonical ID 只取首个 canonical symbol + first valid date。 | 否 | 缺失仍可保留 instrument，并标 `issuer_enrichment_missing` quality flag |
| Nasdaq `Security Name` / SEC `title` | `name` | 原文去首尾空白，不做展示型改写。 | 是 | quarantine |
| Nasdaq `ETF` | `asset_class` | `Y` → `etf`；普通股 → `equity`；指数由单独白名单 fixture 生成。 | 是 | quarantine |
| Nasdaq `Round Lot Size` | `lot_size` | 字符串转 Decimal。 | 否 | `null` |
| source URL + record content | `source.checksum_sha256` | canonical JSON：排序 key、去空白、保留原始字符串数值。 | 是 | quarantine |

稳定 ID / checksum：

- `instrument_id = ins_us_<sha256(UTF-8(canonical_symbol + first_valid_from ISO-8601 date))[:16]>`；seed 无分隔符，且不含 `issuer_key`/CIK。精确输入/输出由 `tests/fixtures/us/normalization/instrument_id_cases.json` 作为 #2/#4 共享 golden；后续如改 ID 语义需 migration/ADR。
- alias 唯一键：`provider_id + source_symbol + valid_from`。
- checksum 只基于业务字段 canonical JSON，不含 retrieved_at。

<a id="daily-bars"></a>

### 2. Daily bars：US equity / index daily OHLCV

| 项 | 设计 |
|---|---|
| Provider role | `us.market.primary` |
| Primary live provider | Twelve Data Basic `GET https://api.twelvedata.com/time_series?symbol=<SPY|QQQ|DIA>&interval=1day`，只接入三个市场代理。 |
| Fallback live candidate | 无生产 fallback；Polygon/Massive、Alpha Vantage 继续保持未批准/fixture-only。 |
| 官方文档 | Twelve Data time-series API、个人/内部使用说明和套餐限制；历史候选的 Polygon/Massive、Alpha Vantage 文档仍仅作调研证据。 |
| Base URL / endpoint | Twelve Data：`GET https://api.twelvedata.com/time_series?symbol=<symbol>&interval=1day&start_date=<date>&end_date=<date>&apikey=<secret>`。 |
| 认证 | Twelve Data 需 API key；key 只能来自运行时 Secret Manager，不进 Git、日志或 fixture。 |
| 分页 | Twelve Data 日线以单 symbol、日期窗口请求，无 cursor；worker 限制窗口/条数并拒绝重复或倒序窗口。 |
| 时区 | Twelve Data `1day` 以交易所本地交易日给出；统一转 UTC，`trading_date` 保留 `America/New_York` 本地交易日。 |
| 更新 | EOD worker 在 `us_close` session 后运行；不得在 API 请求中拉行情。 |
| 历史深度 | 取决于 Basic 套餐和当前额度；仅保证 #34 所需的三个 symbol 与两日报告回放范围。 |
| `available_at` basis | 有 provider dissemination timestamp 时用 `provider_disseminated`；否则 `first_seen`。 |
| MVP 状态 | #26 已批准内部 live ingestion 与 canonical facts 存储；#34 才实现 live adapter。禁止外部 LLM、citation、embedding、再分发，以及 scope 外 symbol。 |

公共合同映射：

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| ticker | `canonical_symbol` / `instrument_id` | 先经 instruments alias 解析，不直接信任 symbol。 | 是 | quarantine `SYMBOL_UNRESOLVED` |
| window start/end | `bar_start` / `bar_end` | 美东 session 时间转 UTC；`bar_start < bar_end`。 | 是 | quarantine `INVALID_TIME_ORDER` |
| local session date | `trading_date` | America/New_York 日期；半日市仍为本地交易日。 | 是 | quarantine |
| open/high/low/close | `open/high/low/close` | Decimal(str(value))；校验 `low <= open/close <= high`。 | 是 | quarantine `INVALID_OHLC` |
| volume / turnover | `volume` / `turnover` | 非负 Decimal；缺 turnover 可为 `null`。 | 否 | `null` + quality flag |
| adjusted flag | `adjustment` / `adjustment_as_of` | 两日 MVP 只接受 `raw + 1d`；调整行情进入 Phase 2。 | 是 | unsupported capability |
| provider trade conditions | `quality_flags` | 异常/空 interval 不伪造 0 bar。 | 否 | flag |

Daily bars 的 ID/checksum 使用本文件“公共 identity、checksum 与排序规则”中的 daily bars 行；`bar_id` 不得依赖抓取时间或分页位置。

### 3. Rates / FX / cross-asset market observations

| 项 | 设计 |
|---|---|
| Provider role | `us.rates_fx.primary` |
| Primary | Federal Reserve Board H.15 Selected Interest Rates、H.10 Foreign Exchange Rates。 |
| Secondary | U.S. Treasury official interest-rate XML feeds and Fiscal Data datasets where endpoint is available. |
| Explicit non-primary | FRED / ALFRED API：仅用于发现 series、人工核对和非持久化研究；不进入 MVP ingest。 |
| 官方文档 | Federal Reserve H.15/H.10 releases and Data Download Program；Treasury official interest-rate pages/API docs；FRED terms for exclusion rationale。 |
| Base URL / endpoint | FRB Data Download Program CSV/XML packages；H.15/H.10 current release pages；Treasury XML feed `/resource-center/data-chart-center/interest-rates/pages/xml?data=<dataset>&field_tdr_date_value=<year-or-all>&page=<n>`；Fiscal Data REST endpoint when confirmed. |
| 认证 | FRB/Treasury official public pages一般无 API key；FRED 需要 API key但默认不用。 |
| 分页 | FRB CSV/XML package 无 cursor；Treasury XML `all` 查询按 `page=0..n` 翻页直到无 entry；Fiscal Data REST 支持 filter/sort/page 时按 API docs 实现。 |
| 时区 | Release timestamp 使用发布方声明时区；观测日期是 business date，不伪造午夜发布时刻。 |
| 历史深度 | 依 release/package；Treasury daily yield curve from 1990、bill rates from 2002、long-term rates from 2000、real yield curve from 2003；H.15/H.10 历史通过 DDP/FRED 迁移路径确认。 |
| `available_at` basis | 有官方 release time 时 `provider_disseminated`；否则 `first_seen`。 |
| MVP 状态 | 官方数据可 live-candidate，但两日 provider 仍以 fixture 验证单位、PIT 和 checksum。 |

首批 metric codes：

| 公共 ID | 来源候选 | 公共字段 |
|---|---|---|
| `rate:US:FEDFUNDS` | FRB H.15 effective federal funds | `MarketObservation(scope_type=MARKET, scope_id="US", metric_code="rate.fed_funds.effective", unit="percent")` |
| `rate:US:UST2Y` | FRB H.15 / Treasury yield curve | `metric_code="rate.treasury.2y", unit="percent"` |
| `rate:US:UST10Y` | FRB H.15 / Treasury yield curve | `metric_code="rate.treasury.10y", unit="percent"` |
| `rate:US:UST30Y` | FRB H.15 / Treasury yield curve | `metric_code="rate.treasury.30y", unit="percent"` |
| `fx:GLOBAL:DXY_PROXY` | FRB H.10 / dollar indexes | `metric_code="fx.usd.index_broad", unit="index_point"` |
| `fx:GLOBAL:EURUSD` | FRB H.10 / Treasury FX where available | `metric_code="fx.eurusd", unit="rate"` |

单位规则：

- 百分比使用 `value="5.2", unit="percent"`，不是 `0.052`。
- bp 输入先转百分比，例如 25bp → `value="0.25", unit="percent"`。
- 指数点使用 `unit="index_point"`，`currency=null`。
- 日期级数据使用 `period_start/period_end` 或 `observed_at` 的业务日语义，不伪造精确发布时间。

Rates/FX 公共合同映射：

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| release / dataset name | `source.source_name` | 保存官方 release 名称，如 H.15/H.10/Treasury。 | 是 | quarantine |
| series code / row label | `metric_code` | 使用本节首批 metric codes；新增 code 必须补 fixture 和合同测试。 | 是 | quarantine `METRIC_UNRESOLVED` |
| observation date | `period_start` / `period_end` / `observed_at` | 日期级业务时点；不伪造午夜发布时间。 | 是 | quarantine `TIMEZONE_REQUIRED` if timestamp lacks zone |
| value | `value` | Decimal(str(raw_value))；`--`/`N/A` → `null` + missing reason。 | 否 | `null` + quality flag |
| unit / scale | `unit` / `currency` | percent、rate、index_point；bp 转 percent。 | 是 | quarantine `UNIT_REQUIRED` |
| release timestamp or fetch completion | `available_at` / `availability_basis` | 有官方发布时间则 `provider_disseminated`；否则 `first_seen`。 | 是 | quarantine |
| source row identity | `observation_id` / `source.checksum_sha256` | 按公共 identity/checksum 表生成。 | 是 | quarantine |

### 4. Macro observations / releases

| 项 | BLS | BEA |
|---|---|---|
| Provider role | `us.macro.primary` | `us.macro.primary` |
| 数据 | CPI、PPI、nonfarm payrolls、unemployment、wages 等 | GDP、PCE、personal income、NIPA 等 |
| 官方文档 | BLS Public Data API developer docs | BEA Data API user guide |
| Base URL / endpoint | `https://api.bls.gov/publicAPI/v2/timeseries/data/` | `https://apps.bea.gov/api/data` |
| 认证 | 未注册 API Version 1 可无 key；注册 API Version 2 需 registration key，且不得提交。 | 注册后获取 36-character `UserID`；不得提交。 |
| 请求参数 | `seriesid[]`、`startyear`、`endyear`、`registrationkey`。 | `UserID`、`method=GetData`、`DataSetName`、dataset-specific params、`ResultFormat=JSON`。 |
| 限流 | BLS FAQ：注册 API Version 2 最多 50 series、20 年、500 queries/day；未注册 API Version 1 为 25 series、10 年、25 queries/day；两者均按 50 requests/10 seconds 控制。 | BEA：100 requests/min、100 MB/min、30 errors/min，429 带 `Retry-After`。 |
| 分页 | 按 series/year window 切分；无平台 cursor。 | 按 dataset params/window 切分；避免 `ALL` 大范围。 |
| 更新时间 | 按各 agency release calendar；BLS API 文档说明 API 可能有发布后滞后。 | 按 BEA release schedule；API 数据随官方发布更新。 |
| 历史深度 | BLS API 限制单次窗口；历史可分段回补。 | 取决于 dataset/table。 |
| revision | BLS 需要根据 series 注释和后续 observation 版本保存 vintage；BEA GDP/PCE 必须新增 vintage，不覆盖旧值。 |
| `available_at` basis | 没有 API dissemination proof 时用平台 `first_seen`；release calendar 只填 `released_at/scheduled_at`。 | 同左。 |
| MVP 状态 | `fixture-only` adapter；live smoke 需 key。 | `fixture-only` adapter；live smoke 需 UserID。 |

首批 macro series：

| `series_id` | 来源 | 口径 |
|---|---|---|
| `macro:US:BLS:CPI_ALL_ITEMS` | BLS CPI | CPI all urban consumers，level/index 与 YoY 分成不同 transformation。 |
| `macro:US:BLS:UNEMPLOYMENT_RATE` | BLS CPS | Unemployment rate，unit percent。 |
| `macro:US:BLS:NONFARM_PAYROLLS` | BLS CES | Payroll employment level/change，单位按上游明确转换。 |
| `macro:US:BEA:GDP` | BEA NIPA | GDP level；修订新增 vintage。 |
| `macro:US:BEA:PCE_PRICE_INDEX` | BEA NIPA | PCE price index / inflation transformation 分 series。 |

公共合同映射：

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| series ID / table + line | `MacroSeries.series_id` / `code` | 固定 `macro:US:<AUTHORITY>:<CODE>`。 | 是 | quarantine |
| period | `period_start` / `period_end` | 月/季/年按实际覆盖期；左闭右开思想用于查询。 | 是 | quarantine |
| value | `value` | Decimal(str(value))；`--`、`N/A` → `null` + missing reason。 | 否 | `null` + quality flag |
| units / footnotes | `unit` / `transformation` / `quality_flags` | level、mom/qoq/yoy、annualized 不混用。 | 是 | quarantine `UNIT_REQUIRED` |
| release calendar time | `MacroRelease.scheduled_at` / `released_at` | 没有精确 release time 不伪造；使用 date precision 或 first_seen。 | 否 | `released_at=null` |
| first retrieval time | `available_at` | 无法证明 provider dissemination 时用 `first_seen`。 | 是 | quarantine |
| revised value | `vintage_id` / `revision_no` / `supersedes_observation_id` | 每次修订新增版本，不原位覆盖。 | 是 | quarantine |

Macro ID/checksum 使用本文件“公共 identity、checksum 与排序规则”中的 macro rows；BLS/BEA release calendar 只能填 `scheduled_at`，不能单独作为 PIT `available_at` 证明。

### 5. SEC filing metadata

| 项 | 设计 |
|---|---|
| Provider role | `us.filings.primary` |
| Primary | SEC EDGAR Data APIs and index files。 |
| 官方文档 | SEC EDGAR Application Programming Interfaces；Accessing EDGAR Data；Developer Resources fair access。 |
| Base URL / endpoint | `https://data.sec.gov/submissions/CIK##########.json`；`https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`；bulk submissions ZIP `https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip`；EDGAR daily/full index directories；`https://www.sec.gov/files/company_tickers.json`。 |
| 认证 | 不需要 API key；必须设置可识别 User-Agent；遵守 10 requests/second fair-access 上限。 |
| 分页 | submissions JSON 中近期 filings 在主文件，历史 filing files 按 SEC JSON 引用追加；bulk ZIP nightly snapshot 用文件 checksum；index files 按日期/quarter 切分。 |
| 时区 | SEC `acceptanceDateTime` 通常为 Eastern time语义；adapter 必须解析为 aware datetime 并转 UTC。 |
| 更新 | SEC Data APIs JSON 结构日内实时更新。 |
| 历史深度 | EDGAR index files 自 1994Q3 起；company submissions 历史由 SEC endpoint 和 archived file 控制。 |
| `available_at` basis | `provider_disseminated` if `acceptanceDateTime`/dissemination time is present and trusted；否则 `first_seen`。 |
| MVP 状态 | `live-candidate` for metadata；两日 fixture 覆盖 8-K、10-Q、10-K metadata。不保存完整 filing body。 |

SEC → NewsEvent / filing metadata 映射：

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| `accessionNumber` | `provider_record_id` / `news_id` seed | 稳定 ID：`news_us_sec_<accessionNumber normalized>`。 | 是 | quarantine |
| `form` | `topics` / `VendorAnnotation` 不使用 | `8-K` → `corporate_action` 或 `earnings` 等规则后续 #6 fixture 固定；不生成情绪。 | 是 | flag if unknown |
| `filingDate` | `published_at` fallback | 只有日期时不伪造分钟级发布时间。 | 是 | quarantine |
| `acceptanceDateTime` | `available_at` / `availability_basis` candidate | 解析为 UTC；若可信则 `availability_basis=provider_disseminated`。不得写入平台 `first_seen_at`。 | 否 | 用 retrieved_at 作为 `first_seen_at`，并设置 `availability_basis=first_seen` |
| `primaryDocument` / filing URL | `canonical_url` / `source_url` | SEC archive URL。 | 是 | quarantine |
| company CIK/ticker | `entities` | `entity_type="company"` 或 `instrument` if alias resolved。 | 否 | empty list + flag |
| form description | `title` | headline-only event：`SEC filing: <form> <company>`。 | 是 | generated title + source provenance |

SEC metadata 的 `first_seen_at` 始终表示平台或可信供应商首次看到时间；SEC `acceptanceDateTime` 只可作为 provider dissemination evidence 或 filing event time，不得冒充平台首次看到时间。

### 6. Daily news

| 项 | 设计 |
|---|---|
| Provider role | `us.news.primary` |
| Official tier | SEC filing metadata；BLS/BEA/Fed/Treasury official releases and announcements。 |
| Open URL discovery candidate | GDELT DOC/API or raw files for article URL/headline discovery only, pending rights review。 |
| Licensed media candidate | NewsAPI / Alpha Vantage news / paid newswire vendor，pending contract。 |
| 官方文档 | GDELT data/API docs；NewsAPI docs/terms if evaluated；Alpha Vantage docs/terms if evaluated。 |
| Base URL / endpoint | SEC RSS feeds；GDELT DOC API `GET /api/v2/doc/doc?query=<query>&mode=artlist&maxrecords=<n>&timespan=<window>&format=json`；NewsAPI `GET /v2/everything?q=<query>&from=<iso>&to=<iso>&language=<lang>&pageSize=<n>&page=<n>` only after procurement. |
| 认证 | Official/GDELT public endpoints generally no key; NewsAPI/Alpha Vantage require API key. |
| 分页 | SEC RSS source feed pages/entries；GDELT `maxrecords` + time window，不承诺 cursor；NewsAPI `page/pageSize`。所有 provider 都必须有 duplicate page fixture。 |
| 时区 | `published_at` from source; if only date is available use date precision and `available_at=first_seen_at`。 |
| 内容模式 | 默认 `headline` / `snippet`；commercial body 默认 `null`。 |
| `available_at` basis | `first_seen` unless official/source dissemination timestamp is independently available. |
| MVP 状态 | `fixture-only` for licensed media; official SEC/release headlines can be live-candidate。 |

新闻授权默认：

- Official releases：可保存 metadata/headline/snippet；body 只在来源明确允许且负责人批准后保存。
- GDELT：rights approval 前不启用 live ingest，不发送外部 LLM；即使后续批准，也只允许保存 URL/source/time/topic 等 metadata，不抓原站正文。
- NewsAPI/Alpha Vantage/newswire：无合同前 `storage_allowed=false` for live content，MVP 只用合成 fixture；不得把其示例内容复制为 fixture。
- `vendor_annotations` 只保存供应商原始标签；不把 sentiment 平均成平台交易因子。

Daily news 公共合同映射：

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| source article/release ID or URL | `news_id` / `source.provider_record_id` | 优先 provider stable ID；否则 canonical URL/hash identity。 | 是 | quarantine `NEWS_IDENTITY_MISSING` |
| title/headline | `title` | Unicode NFKC/空白规范化只用于去重；保留原始标题语义，否定词不可删除。 | 是 | quarantine |
| description/snippet | `summary` | 仅在授权允许时保存；否则 `null`。 | 否 | `null` |
| article body | `body` | MVP 默认不保存；商业正文无合同必须 `null`。 | 否 | `null` |
| source published time | `published_at` | 解析为 aware UTC；无 timezone 不伪造。 | 是 | quarantine `TIMEZONE_REQUIRED` 或 date precision |
| provider first seen / platform fetch | `first_seen_at` / `available_at` | 无可信 provider first-seen 时用平台 retrieved_at。 | 是 | quarantine |
| publisher/source | `source_name` / `source_tier` | official、licensed_media、other 等按来源登记。 | 是 | quarantine |
| URL | `canonical_url` / `source.source_url` | 去 tracking 参数；保留 source URL。 | 否 | `null` + identity_basis flag |
| topics/entities | `topics` / `entities` | 只用公共 taxonomy；不生成平台情绪。 | 否 | empty list |
| rights | `usage_rights` | 按 source-level rights matrix；受限内容不得通过嵌套字段泄漏。 | 是 | quarantine |
| content hash | `content_hash_sha256` / `source.checksum_sha256` | canonical headline/snippet metadata hash；不含 restricted body。 | 是 | quarantine |

## 权利矩阵

### Source-level rights

| 来源 | storage_allowed | internal_analysis_allowed | external_llm_allowed | embedding_allowed | redistribution_allowed | 依据/到期日 |
|---|---:|---:|---:|---:|---:|---|
| SEC EDGAR metadata / company tickers | true | true | true for metadata/headline only | true for metadata only | true with source attribution; no SEC endorsement | Public SEC access；遵守 fair access；复核 2026-10-23。 |
| Nasdaq Trader symbol directory | false until reviewed | true for fixture/design | false | false | false | Exchange reference data rights未完成复核；live 标 `fixture-only`。 |
| Polygon/Massive market data | false until business agreement | false until business agreement | false | false | false | Market data terms/licensor agreements required；无合同不 live。 |
| Alpha Vantage market/FX/news | false until commercial approval | false until commercial approval | false | false | false | Terms distinguish personal/non-commercial vs commercial use；MVP 仅 fixture/design。 |
| Twelve Data Basic `SPY` / `QQQ` / `DIA` daily bars | true for internal canonical facts only | true for personal/internal analysis only | false | false | false | @Detachm 于 2026-07-27 批准的受限范围；Individual/Basic 不构成外部模型、第三方展示或再分发授权。 |
| Federal Reserve H.15/H.10 direct releases/DDP | true for numeric facts | true | true for numeric facts with source citation | true for numeric facts | true with attribution/no endorsement | Official public statistical releases；DDP retirement path需复核。 |
| Treasury official feeds / Fiscal Data | true for numeric facts | true | true for numeric facts with source citation | true for numeric facts | true with attribution/no endorsement | Official public Treasury data；endpoint availability复核。 |
| BLS Public Data API | true for public observations | true | true for numeric facts with source citation | true for numeric facts | true with attribution/no endorsement | Official public statistics；API key/limits apply。 |
| BEA API | true for public observations | true | true for numeric facts with source citation | true for numeric facts | true with attribution/no endorsement | Official public statistics；UserID/limits apply。 |
| FRED / ALFRED API | false | false for platform ingest | false | false | false | 平台 no-ingest/no-LLM/no-embedding 策略，待逐 series owner rights review 与负责人批准；FRED terms 要求遵守 data owner restrictions。 |
| GDELT | false until rights approval | false until rights approval | false | false | false | 权限不明确按不允许；仅 synthetic fixture。 |
| NewsAPI / licensed media | false until contract | false until contract | false | false | false | Terms prohibit republishing copyrighted material; contract required。 |

### Default `UsageRights` by content type

| Content | storage_allowed | internal_analysis_allowed | external_llm_allowed | embedding_allowed | redistribution_allowed |
|---|---:|---:|---:|---:|---:|
| Official numeric observations | true | true | true | true | true |
| SEC filing metadata/headline | true | true | true | true | true |
| SEC full filing body | false in MVP | false in MVP | false | false | false |
| Licensed media headline/snippet | false until contract | false until contract | false | false | false |
| Licensed media body | false | false | false | false | false |
| Synthetic fixture news | true | true | true | true | false |

## `available_at` basis policy

| 数据类 | `released_at` | `first_seen_at` | `available_at` |
|---|---|---|---|
| Market bars | provider EOD/bar completion time if supplied | platform retrieval completion | `provider_disseminated` if source proves dissemination; otherwise `first_seen` |
| H.15/H.10/Treasury observations | official release timestamp if supplied | platform retrieval completion | same as above; date-only release cannot be used as exact PIT time |
| BLS/BEA observations | official release calendar for scheduled/released metadata | API/platform first retrieval | no API proof → `first_seen` |
| SEC filings | `acceptanceDateTime` if present | platform first retrieval | SEC acceptance time if trusted; otherwise `first_seen` |
| News | source `published_at` | platform first seen | never earlier than first trusted availability evidence |

Any row without a trustworthy availability proof must use `availability_basis=first_seen` and must not be returned for `as_of < first_seen_at`.

## 错误与降级

| 情况 | Provider exception / result | 重试 |
|---|---|---|
| Missing credentials / no contract | health=`not_configured`；dataset marked `fixture-only` | no |
| 401 / invalid key | `ProviderAuthenticationError` | no; alert secret owner |
| 403 / license denied | `ProviderAuthorizationError` | no; mark `LICENSE_RESTRICTION` |
| 429 / Retry-After | `ProviderRateLimitError(retry_after_seconds=...)` | yes after header delay |
| Timeout | `ProviderTimeoutError` | yes with bounded backoff |
| HTML login page / auth wall / risk-control page | `ProviderAuthorizationError` | no; alert account owner |
| Malformed JSON / unexpected non-JSON provider payload | `ProviderSchemaError` | no until schema reviewed |
| Unknown required field rename | `ProviderSchemaError` + schema drift warning | no bulk write null |
| Valid empty upstream response | `ProviderPage(items=[], complete=True)` | no error |
| Empty page with repeated next cursor | `ProviderCursorError` / `INVALID_PAGINATION` after threshold | no infinite loop |
| Expired/invalid provider cursor | `ProviderCursorError` | resume from committed watermark |
| Stale official release | coverage=`stale`; no fake current value | retry by schedule |
| Upstream source conflict | preserve both records and provenance | no arbitrary overwrite |

Fixture-only adapter 只能绑定 `us.<domain>.fixture_contract` 角色以运行合同测试；不得绑定
`us.*.primary`，生产调度必须拒绝该 adapter。

## Fixtures 与测试

Fixture directories for follow-up issues:

- `tests/fixtures/us/instruments/`
- `tests/fixtures/us/market_bars/`
- `tests/fixtures/us/rates_fx/`
- `tests/fixtures/us/macro/`
- `tests/fixtures/us/sec_filings/`
- `tests/fixtures/us/news/`

Minimum fixture names per provider slice:

- `success.json`
- `empty.json`
- `missing_fields.json`
- `auth_failure.json`
- `rate_limited.json`
- `timeout.json`
- `schema_changed.json`
- `duplicate_page.json`

Test IDs required downstream:

- #4 normalization：`SYM-004`～`SYM-010`、`TIME-001`、`TIME-002`、`TIME-005`、`TIME-006`、`TIME-010`、`UNIT-001`、`UNIT-002`、`UNIT-004`、`UNIT-005`、`UNIT-009`
- #6/#7 fixture provider contract：`PRV-001`～`PRV-010`、`PRV-013`、`PRV-015`、`PRV-017`～`PRV-021`；`PRV-011`、`PRV-012`、`PRV-014`、`PRV-016` 需要持久化状态，转交 [#20](https://github.com/Detachm/macro-data-platform/issues/20)。
- News：`NEWS-002`、`NEWS-003`、`NEWS-012`、`NEWS-013`、`NEWS-017`
- PIT：all provider outputs assert `available_at <= as_of`

### #7 US fixture contract evidence

共享合同测试入口为 `tests/contract/test_us_fixture_provider_contract.py`，只使用
`tests/fixtures/us/provider/` 下的合成离线 fixture；其受测试保护的
`manifest.json` 声明所有 fixture 文件、覆盖场景和无凭据约束。复现命令：

```bash
uv run pytest tests/unit tests/contract -m "not live" -q
```

合同用例通过 pytest 参数 ID 直接暴露其测试编号；可用
`pytest tests/contract/test_us_fixture_provider_contract.py --collect-only -q`
追溯每个 `PRV-*`、`PIT-009`、`TIME-005` 和 `NEWS-*` 证据。临时生成的
两页、字段重排和未来记录 fixture 只在测试目录中创建，不会成为可被
生产调度使用的数据源。

| Test ID | Evidence |
|---|---|
| `PRV-001`、`PRV-002`、`PRV-005`、`PRV-013` | 共享 suite 对全纵向切片的稳定输出、provenance、checksum 和 query 不变性断言。 |
| `PRV-003` | 公开 `fetch_news` 正常两页回归：合并结果完整、无重复，末页 `cursor=null`；另有跨页重复 provider record 的拒绝回归。 |
| `PRV-015` | 空页阈值 fixture/unit test。 |
| `PRV-004` | shared contract test 验证 bars 的 `[start, end)` 边界。 |
| `PRV-006` | mixed invalid record 被 quarantine，合法记录继续输出。 |
| `PRV-007`～`PRV-010`、`PRV-019`～`PRV-021` | 离线 error fixture 必须抛显式错误，绝不能变成空页。 |
| `PRV-011`、`PRV-012`、`PRV-014`、`PRV-016` | 不适用：fixture adapter 没有 unsupported-PIT capability 状态、持久化 raw audit record、事务性 repository 或 committed watermark store。分别需要历史 PIT capability、DST 原始值审计、写入成功后丢响应的幂等重试、cursor 过期后的 watermark 恢复；统一转交 [#20](https://github.com/Detachm/macro-data-platform/issues/20)。 |
| `TIME-005` | `dst_offset.json` 使用 `America/New_York` 夏令时 `09:30-04:00` 输入，输出 UTC 并保持正确 trading date。 |
| `PRV-017` | 同一 provider fixture 用原始及递归 JSON key 重排文本各执行一次公开 fetch，输出 checksum 必须相同。 |
| `PRV-018` | fixture-only `not_configured` health regression test。 |
| `NEWS-002`、`NEWS-003`、`NEWS-012`、`NEWS-013`、`NEWS-017` | 仅经公开 `fetch_news` 验证 canonical URL、标题 fingerprint、headline-only、空 vendor annotations、外部 LLM 权利清洗。 |
| `PIT-009` | 为 bars、market observations、macro observations、macro releases、news 分别注入未来 `available_at` 记录，以较早 `as_of` 查询；每个输出都必须过滤未来记录。 |

默认 CI 不运行 live smoke；仅在 Phase 2 获得来源批准和显式凭据后，才可执行
`pytest -m live` 中定义的最小请求。

Fixture policy:

- Use synthetic values or heavily transformed public facts; no vendor examples copied verbatim.
- No token, Cookie, account ID, email, full news body, or restricted summary.
- Market data fixture values are synthetic even when shaped like Polygon/Alpha Vantage responses.
- SEC fixture may use synthetic accession numbers unless a public accession is needed for parser realism.

## 在线 smoke 方法（Phase 2）

No live smoke should run by default in PR CI. Use `pytest -m live` only with explicit credentials and source approval.

| Provider | Minimal live smoke | Cost/risk control |
|---|---|---|
| SEC EDGAR | Fetch one known CIK submissions JSON; assert no body fetch, User-Agent set, rate below 1 rps in tests. | Public; obey 10 rps global cap. |
| BLS | Fetch one CPI/unemployment series for two years. | Requires registered key; skip if missing. |
| BEA | `GetDatasetList` and one NIPA row/window. | Requires UserID; stay well below 100/min. |
| FRB H.15/H.10 | Download latest/current release or package metadata only. | Public; no aggressive polling. |
| Polygon/Massive | One prior-day bar for a configured ticker. | Only after business license; skip otherwise. |
| Twelve Data Basic | One prior-day `1day` bar for one of `SPY`、`QQQ`、`DIA`。 | Only with runtime key, explicit `live` marker and Basic quota budget; no external LLM/citation path. |
| News provider | One short headline/snippet query. | Only after contract; body remains off. |

## 运行指标

- `provider_request_total{provider_role,dataset,region}`
- `provider_request_error_total{provider_role,dataset,code}`
- `provider_request_duration_ms`
- `provider_records_fetched_total`
- `provider_records_rejected_total`
- `provider_last_success_at`
- `provider_data_latest_available_at`
- `provider_stale_seconds`
- `schema_validation_failure_total`
- `fallback_activation_total`

Quality thresholds for US MVP fixture providers:

- required public contract fields complete: 100%
- duplicate provider record IDs after replay: 0
- PIT future leak: 0
- Decimal roundtrip error: 0
- secret/restricted content in fixtures/logs: 0

## 退出方案

- Revoked key/contract：set provider health to `not_configured` or `down`; stop worker role; do not delete canonical facts until retention decision is approved.
- Source license conflict：mark source as disabled, quarantine newly fetched records, open follow-up Issue for replacement.
- FRED accidental ingestion：stop worker, quarantine records, purge cached/raw FRED payloads if required, regenerate affected derived/context outputs.
- Commercial news/market data breach：notify data owner, revoke key, rotate secret, remove restricted fixture/log artifacts, and document incident.

## 参考来源

- SEC EDGAR APIs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- SEC Accessing EDGAR Data / fair access: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
- SEC Developer Resources: https://www.sec.gov/about/developer-resources
- Nasdaq Trader Symbol Directory definitions: https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs
- Polygon/Massive Stocks Aggregates docs: https://massive.com/docs/rest/stocks/aggregates/custom-bars
- Polygon/Massive Market Data Terms: https://massive.com/terms/market_data_terms.pdf
- Alpha Vantage API docs: https://www.alphavantage.co/documentation/
- Alpha Vantage Terms: https://www.alphavantage.co/terms_of_service/
- Alpha Vantage support / limits: https://www.alphavantage.co/support/
- Twelve Data API docs: https://twelvedata.com/docs
- Twelve Data personal/internal use policy: https://support.twelvedata.com/en/articles/5332349-commercial-and-personal-usage
- Twelve Data individual pricing: https://twelvedata.com/pricing
- Federal Reserve H.15: https://www.federalreserve.gov/releases/h15/
- Federal Reserve H.10: https://www.federalreserve.gov/releases/h10/
- Federal Reserve Data Download Program: https://www.federalreserve.gov/datadownload/
- Treasury Fiscal Data API documentation: https://fiscaldata.treasury.gov/api-documentation/
- Treasury Daily Interest Rate XML Feed: https://home.treasury.gov/treasury-daily-interest-rate-xml-feed
- BLS Public Data API FAQ / quotas: https://www.bls.gov/developers/api_faqs.htm
- BLS API Version 2 signature: https://www.bls.gov/developers/api_signature_v2.htm
- BEA API signup/docs: https://apps.bea.gov/api/signup/
- BEA API user guide: https://apps.bea.gov/api/_pdf/bea_web_service_api_user_guide.pdf
- FRED API terms: https://fred.stlouisfed.org/docs/api/terms_of_use.html
- FRED legal notices: https://fred.stlouisfed.org/legal/
- GDELT data/API: https://www.gdeltproject.org/data.html
- NewsAPI docs/terms: https://newsapi.org/docs and https://newsapi.org/terms

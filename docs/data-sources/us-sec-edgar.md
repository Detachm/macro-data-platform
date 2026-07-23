# 数据源登记：SEC EDGAR APIs and SEC RSS

## 所有权

- Owner：@kazming666
- 区域：US
- Provider role：`us.filings.primary`、`us.news.primary`、`us.instruments.primary` enrichment
- 数据集：SEC filing metadata / company ticker crosswalk / official news metadata
- 官方文档：https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- 账号负责人：@Detachm（无需 API key；必须配置可识别 User-Agent/contact）
- 采购/合同负责人：@Detachm（public source；仍需 fair-access 复核）
- 当前状态：metadata `live-candidate`；filing body 不在 MVP 范围。
- 首次批准日期与复核日期：未批准；待 @Detachm 批准后 30 天复核，之后每季度复核。
- 关联 Issue：[#2](https://github.com/Detachm/macro-data-platform/issues/2)

## 接口与覆盖

- Base URL 与端点：`https://data.sec.gov/submissions/CIK##########.json`、`https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`、`https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip`、`https://www.sec.gov/files/company_tickers.json`、SEC RSS feeds。
- 请求参数、分页/cursor：CIK path parameter；submissions JSON 引用历史 files；bulk ZIP nightly snapshot；RSS 按 feed entries。
- 频率、时区和上游时间字段：SEC submissions real-time updated；`acceptanceDateTime` 可作为 provider dissemination evidence，不能写成平台 `first_seen_at`。
- 历史深度、修订策略：recent submissions + historical files/bulk index；metadata version by accession number。
- 代码、单位、币种和空值规则：CIK/ticker 只做 issuer/instrument enrichment；无行情数值。
- 限流、并发、超时和重试要求：遵守 SEC fair access，自动化请求全局不超过 10 requests/second；测试低于 1 rps。

## 权利矩阵

| 权利 | 允许 | 依据/到期日 |
|---|---:|---|
| storage_allowed | true | Public SEC metadata；遵守 fair access |
| internal_analysis_allowed | true | metadata/headline only |
| external_llm_allowed | true | metadata/headline only；body excluded |
| embedding_allowed | true | metadata/headline only；body excluded |
| redistribution_allowed | true | attribution/no-endorsement controls |

## 公共合同映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| `accessionNumber` | `news_id` / `source.provider_record_id` | `news_us_sec_<normalized_accession>`；稳定不依赖抓取时间。 | 是 | quarantine |
| `form` | `topics` | `8-K`/`10-Q`/`10-K` 等映射为 official filing event topic；不生成情绪。 | 是 | flag unknown |
| `filingDate` | `published_at` fallback | date-only 不伪造 intraday timestamp。 | 是 | quarantine |
| `acceptanceDateTime` | `available_at` candidate | 若可信则 `availability_basis=provider_disseminated`；不得写成平台 `first_seen_at`。 | 否 | `available_at=first_seen` |
| `primaryDocument` | `canonical_url` / `source.source_url` | SEC archive URL。 | 是 | quarantine |
| CIK/ticker/title | `entities` / `title` | CIK as company entity；ticker only after alias resolution。 | 否 | empty + flag |

Identity basis：`accessionNumber + form + cik`。
`first_seen_at` basis：platform retrieval time unless trusted provider first-seen exists。
Checksum：canonical SEC metadata JSON，不含 retrieved_at。
Source URL：SEC archive URL / data.sec.gov endpoint。
稳定排序键：`published_at DESC, news_id DESC`。

## 失败与降级

- Missing User-Agent/contact：health=`not_configured`。
- 401 / invalid request identity：`ProviderAuthenticationError`，不重试。
- 403/fair-access block：`ProviderAuthorizationError`；reduce rate and alert owner。
- 429/too many requests：`ProviderRateLimitError`。
- Timeout：`ProviderTimeoutError`，bounded retry；不推进 watermark。
- HTML login/auth wall/risk-control page：`ProviderAuthorizationError`，不得当成空数据。
- Malformed JSON / unexpected non-JSON provider payload：`ProviderSchemaError`。
- CIK not found：empty page for unknown resource vs 404 for resolve endpoint。
- Schema drift/missing required accession fields：`ProviderSchemaError`。
- Empty page loop / repeated feed page：`ProviderCursorError` / `INVALID_PAGINATION` after threshold。
- Cursor expiry：RSS/feed pagination 或 files references 过期时映射为 `ProviderCursorError`。
- Filing body requested accidentally：policy violation; block request path。

## Fixtures 与测试

- Fixture 目录：`tests/fixtures/us/sec_edgar/`。
- 最低 fixture 集：`success.json`、`empty.json`、`missing_fields.json`、`auth_failure.json`、`rate_limited.json`、`timeout.json`、`schema_changed.json`、`duplicate_page.json`。
- 对账来源与容差：MVP 使用 synthetic golden fixture，Decimal 往返误差为 0；Phase 2 live 对账只使用已批准来源，市场价格容差按工程规范 1bp，官方宏观/利率同源重放 checksum 必须一致。
- 测试 ID：`PRV-001`～`PRV-021` applicable；`NEWS-012`、`NEWS-017`、PIT `available_at <= as_of`。
- 在线 smoke：one known CIK submissions JSON, no body fetch, <1 rps in tests。
- 脱敏：public accession metadata ok；no full filing body/exhibits in fixture unless explicitly approved。

## 运行指标与退出方案

- 告警接收人：@kazming666；授权、采购、密钥或外部 LLM 传输问题抄送 @Detachm。
- 指标：request_total/error_total/duration、records_fetched/rejected、last_success_at、schema_validation_failure_total。
- freshness：submissions metadata expected near-real-time; stale if source lag exceeds runbook threshold。
- 退出：stop SEC role if blocked; keep public metadata canonical rows; purge accidental body/raw payloads。

# 数据源登记：U.S. Treasury interest-rate XML feeds

## 所有权

- Owner：@kazming666
- 区域：US
- Provider role：`us.rates_fx.primary`
- 数据集：market_observations
- 官方文档：https://home.treasury.gov/treasury-daily-interest-rate-xml-feed
- 账号负责人：@Detachm（无 API key）
- 采购/合同负责人：@Detachm（public official data）
- 当前状态：official numeric data `live-candidate`；MVP provider 仍先 fixture-backed。
- 首次批准日期与复核日期：未批准；待 @Detachm 批准后 30 天复核，之后每季度复核。
- 关联 Issue：[#2](https://github.com/Detachm/macro-data-platform/issues/2)

## 接口与覆盖

- Base URL 与端点：`/resource-center/data-chart-center/interest-rates/pages/xml?data=<dataset>&field_tdr_date_value=<year-or-all>&page=<n>`。
- 请求参数、分页/cursor：`data=daily_treasury_yield_curve|daily_treasury_bill_rates|...`；`all&page=N` until no entries。
- 频率、时区和上游时间字段：daily rows；precise row publish timestamp 未确认，使用 `first_seen`。
- 历史深度、修订策略：yield curve from 1990；bill rates from 2002；long-term from 2000；real yield from 2003。
- 代码、单位、币种和空值规则：percent value 不转成 ratio；missing rows 保持 null/quality flag。
- 限流、并发、超时和重试要求：官方未给数值；低频抓取 + bounded backoff。

## 权利矩阵

| 权利 | 允许 | 依据/到期日 |
|---|---:|---|
| storage_allowed | true | official public numeric facts |
| internal_analysis_allowed | true | official public numeric facts |
| external_llm_allowed | true | numeric facts with source citation |
| embedding_allowed | true | numeric facts |
| redistribution_allowed | true | attribution/no-endorsement controls |

## 公共合同映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| `data` dataset + maturity column | `metric_code` | e.g. treasury yield 2Y/10Y/30Y；新增 maturity 补 taxonomy/fixture。 | 是 | quarantine `METRIC_UNRESOLVED` |
| row date | `period_start` / `period_end` / `observed_at` | 日期级 observation；不伪造发布时间。 | 是 | quarantine |
| rate value | `MarketObservation.value` | Decimal(str(raw)); percent value 保持 percent。 | 否 | `null` + quality flag |
| unit | `unit` | `percent`。 | 是 | quarantine `UNIT_REQUIRED` |
| XML entry/source metadata | `source` | source URL、retrieved_at、checksum 完整。 | 是 | quarantine |

Identity basis：`region + scope_type + scope_id + metric_code + observed_at + provider_id`。
`available_at` basis：无 precise publish timestamp 时用平台 `first_seen`。
Checksum：canonical XML entry JSON，不含 retrieved_at/page number。
Source URL：Treasury XML endpoint with data/year params。
稳定排序键：`observed_at ASC, observation_id ASC`。

## 失败与降级

- 401 / protected endpoint requires auth：`ProviderAuthenticationError`，不重试。
- 403 / access denied / auth wall：`ProviderAuthorizationError`，不重试。
- 429 / rate limit：`ProviderRateLimitError`，按 header/schedule 退避。
- Timeout/network failure：`ProviderTimeoutError`，bounded retry；不推进 watermark。
- Empty `all&page=N` after previous entries：complete page end。
- Empty first page unexpectedly：warning + coverage unavailable/stale。
- Empty page loop / repeated same `page=N` result：`ProviderCursorError` / `INVALID_PAGINATION` after threshold。
- Cursor expiry：page-based pagination 无 opaque cursor；若 adapter cursor 过期，映射为 `ProviderCursorError`。
- HTML login/auth wall/risk-control page：`ProviderAuthorizationError`，不得当成空数据。
- Malformed XML/JSON / unexpected non-JSON provider payload：`ProviderSchemaError`。
- XML schema drift：`ProviderSchemaError`。
- Missing maturity/value：row quarantine，不写 0。

## Fixtures 与测试

- Fixture 目录：`tests/fixtures/us/treasury_interest_rates/`。
- 最低 fixture 集：`success.json`、`empty.json`、`missing_fields.json`、`auth_failure.json`、`rate_limited.json`、`timeout.json`、`schema_changed.json`、`duplicate_page.json`。
- 对账来源与容差：MVP 使用 synthetic golden fixture，Decimal 往返误差为 0；Phase 2 live 对账只使用已批准来源，市场价格容差按工程规范 1bp，官方宏观/利率同源重放 checksum 必须一致。
- 测试 ID：`PRV-001`～`PRV-021` applicable；`UNIT-001`、`UNIT-009`、PIT tests。
- 在线 smoke：one recent year or one month query；low-frequency。
- 脱敏：public numeric data only；no secrets。

## 运行指标与退出方案

- 告警接收人：@kazming666；授权、采购、密钥或外部 LLM 传输问题抄送 @Detachm。
- 指标：request_total/error_total/duration、records_fetched/rejected、last_success_at、data_latest_available_at、provider_stale_seconds。
- freshness：daily business-day cadence；late source marks stale。
- 退出：disable dataset config；retain public numeric facts；raw/cache per retention.

# 数据源登记：BLS Public Data API

## 所有权

- Owner：@kazming666
- 区域：US
- Provider role：`us.macro.primary`
- 数据集：macro_series / macro_observations / macro_releases
- 官方文档：https://www.bls.gov/developers/api_signature_v2.htm；配额说明：https://www.bls.gov/developers/api_faqs.htm
- 账号负责人：@Detachm（registration key 待配置；不得提交）
- 采购/合同负责人：@Detachm（public statistics）
- 当前状态：official numeric data `live-candidate`；MVP provider 仍先 fixture-backed。
- 首次批准日期与复核日期：未批准；待 @Detachm 批准后 30 天复核，之后每季度复核。
- 关联 Issue：[#2](https://github.com/Detachm/macro-data-platform/issues/2)

## 接口与覆盖

- Base URL 与端点：`GET /publicAPI/v2/timeseries/data/{series_id}`；`POST /publicAPI/v2/timeseries/data/`。
- 请求参数、分页/cursor：`seriesid[]`、`startyear`、`endyear`、`registrationkey`；按 year window 切分，无 cursor。
- 频率、时区和上游时间字段：release calendar uses Eastern Time；API observations are period-level，actual `available_at` 默认 `first_seen`。
- 历史深度、修订策略：注册 API Version 2 的单次窗口为 20 年；未注册 API Version 1 的单次窗口为 10 年；macro revisions 新增 vintage。
- 代码、单位、币种和空值规则：series-specific unit/transformation；`--`/`N/A` → null + quality flag。
- 限流、并发、超时和重试要求：BLS FAQ 列明注册 API Version 2 为 500 queries/day、50 series/query、20 years/query；未注册 API Version 1 为 25 queries/day、25 series/query、10 years/query；两者均为 50 requests/10 seconds。注册是 Version 2 的扩容路径，不将 Version 1 无 key 能力表述为 Version 2。

## 权利矩阵

| 权利 | 允许 | 依据/到期日 |
|---|---:|---|
| storage_allowed | true | official public statistics |
| internal_analysis_allowed | true | official public statistics |
| external_llm_allowed | true | numeric facts with source citation |
| embedding_allowed | true | numeric facts |
| redistribution_allowed | true | citation/no-endorsement controls |

## 公共合同映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| `seriesID` | `MacroSeries.series_id` / `code` | `macro:US:BLS:<CODE>`；官方 series ID 保留在 source metadata。 | 是 | quarantine `SERIES_UNRESOLVED` |
| `year` + `period` | `period_start` / `period_end` | 月/季/年按 series metadata 展开。 | 是 | quarantine |
| `value` | `MacroObservation.value` | Decimal(str(raw)); missing token → `null` + quality flag。 | 否 | `null` |
| series/unit metadata | `unit` / `transformation` | CPI level 与 YoY 等分开 series。 | 是 | quarantine `UNIT_REQUIRED` |
| release calendar row | `MacroRelease.scheduled_at` | Eastern Time；只作 scheduled/release metadata。 | 否 | `released_at=null` |
| footnotes/aspects | `quality_flags` | 保存缺失、preliminary、revision hints。 | 否 | empty |

Identity basis：`series_id + period_end + vintage_id + provider_id`；release 使用 `series_id + scheduled_at + period_end + provider_id`。
`available_at` basis：API 无精确 dissemination timestamp 时用平台 `first_seen`。
Checksum：canonical BLS series row JSON，不含 registration key、retrieved_at。
Source URL：BLS API endpoint or release-calendar URL without key。
稳定排序键：observations `period_end ASC, series_id ASC, available_at ASC`；releases `scheduled_at ASC, release_id ASC`。

## 失败与降级

- Missing registration key：可走 unregistered fixture/live smoke 降级，但必须标 capability 限制。
- 401 / key invalid：`ProviderAuthenticationError`，不重试。
- 403 / authorization denied / auth wall：`ProviderAuthorizationError`，不重试。
- 429 / daily quota：`ProviderRateLimitError`；不当成空数据。
- Timeout：`ProviderTimeoutError`，bounded retry；不推进 watermark。
- HTML login/auth wall/risk-control page：`ProviderAuthorizationError`，不得当成空数据。
- Malformed JSON / unexpected non-JSON provider payload：`ProviderSchemaError`。
- API status error / schema drift：`ProviderSchemaError`。
- Empty page loop / repeated provider window：`ProviderCursorError` / `INVALID_PAGINATION` after threshold。
- Cursor expiry：BLS MVP 无 cursor；若后续 provider token 过期，映射为 `ProviderCursorError`。
- 超过 year/series window：拆分请求；不拉全量。

## Fixtures 与测试

- Fixture 目录：`tests/fixtures/us/bls/`。
- 最低 fixture 集：`success.json`、`empty.json`、`missing_fields.json`、`auth_failure.json`、`rate_limited.json`、`timeout.json`、`schema_changed.json`、`duplicate_page.json`。
- 对账来源与容差：MVP 使用 synthetic golden fixture，Decimal 往返误差为 0；Phase 2 live 对账只使用已批准来源，市场价格容差按工程规范 1bp，官方宏观/利率同源重放 checksum 必须一致。
- 测试 ID：`PRV-001`～`PRV-021` applicable；`UNIT-001`、`UNIT-009`、PIT revision tests。
- 在线 smoke：一个 CPI/unemployment 小窗口；缺 key 或 quota 不阻塞 PR。
- 脱敏：移除 registration key 和完整 request URL。

## 运行指标与退出方案

- 告警接收人：@kazming666；授权、采购、密钥或外部 LLM 传输问题抄送 @Detachm。
- 指标：request_total/error_total/duration、records_fetched/rejected、last_success_at、data_latest_available_at、provider_stale_seconds。
- freshness：按 release calendar；迟到标 stale。
- 退出：撤销 key、停 worker role；public observations 保留，raw 按 retention 删除。

# 数据源登记：Polygon/Massive market data

## 所有权

- Owner：@kazming666
- 区域：US
- Provider role：`us.market.primary`
- 数据集：daily bars / reference tickers / optional licensed news
- 官方文档：https://massive.com/docs/rest/stocks/aggregates/custom-bars
- 账号负责人：@Detachm（待采购；不得提交 API key）
- 采购/合同负责人：@Detachm（business agreement required）
- 当前状态：`fixture-only`，无合同前不持久化 live market data。
- 首次批准日期与复核日期：未批准；待 @Detachm 批准后 30 天复核，之后每季度复核。
- 关联 Issue：[#2](https://github.com/Detachm/macro-data-platform/issues/2)

## 接口与覆盖

- Base URL 与端点：`GET /v2/aggs/ticker/{stocksTicker}/range/{multiplier}/{timespan}/{from}/{to}`；news endpoint 另行采购后评估。
- 请求参数、分页/cursor：`multiplier=1`、`timespan=day`、`from`、`to`、`adjusted=false` for MVP raw bars、`sort=asc`、`limit`；响应 `next_url` 作为 provider cursor。
- 频率、时区和上游时间字段：aggregate periods use Eastern Time；daily bars 按 `America/New_York` session 计算 trading date。
- 历史深度、修订策略：plan-specific；未采购不承诺。
- 代码、单位、币种和空值规则：ticker 必须先经 instrument alias 解析；OHLCV 用 Decimal(str(raw))。
- 限流、并发、超时和重试要求：plan-specific；429 映射为 `ProviderRateLimitError`。

## 权利矩阵

| 权利 | 允许 | 依据/到期日 |
|---|---:|---|
| storage_allowed | false | 合同未确认 |
| internal_analysis_allowed | false | 合同未确认 |
| external_llm_allowed | false | market data/news rights 未确认 |
| embedding_allowed | false | 未授权 |
| redistribution_allowed | false | 未授权 |

## 公共合同映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| ticker | `canonical_symbol` / `instrument_id` | 先经 alias 解析；不新增私有 symbol DTO。 | 是 | quarantine `SYMBOL_UNRESOLVED` |
| aggregate timestamp `t` | `bar_start` / `bar_end` / `trading_date` | Eastern Time session rules；UTC 输出。 | 是 | quarantine `INVALID_TIME_ORDER` |
| `o/h/l/c` | `open/high/low/close` | Decimal(str(raw)); 校验 OHLC。 | 是 | quarantine `INVALID_OHLC` |
| `v`, `vw` | `volume`, `vwap` | 非负 Decimal；缺失为 null。 | 否 | `null` + quality flag |
| `n` / conditions | `quality_flags` | 保存异常交易/聚合质量信息。 | 否 | empty |
| response `next_url` | provider cursor | 不透明 cursor；不得暴露 API key。 | 否 | null |

Identity basis：`instrument_id + interval + bar_start + adjustment + provider_id`。
`available_at` basis：无 documented dissemination timestamp 时用 `first_seen`；plan delay 写入 source config。
Checksum：canonical aggregate row JSON，不含 API key、retrieved_at、page position。
Source URL：aggregate endpoint without key。
稳定排序键：`bar_end ASC, instrument_id ASC, bar_id ASC`。

## 失败与降级

- Missing contract/key：health=`not_configured`。
- 401/403/license denied：auth/authorization error，不重试。
- 429/plan limit：`ProviderRateLimitError`；honor retry-after if present。
- `next_url` repeats empty pages：`ProviderCursorError` after threshold。
- HTML login/auth wall/risk-control page：`ProviderAuthorizationError`。
- Malformed JSON / unexpected non-JSON payload、schema drift：`ProviderSchemaError`。
- Main source unavailable with no approved fallback：coverage=`unavailable`，不偷返旧数据。

## Fixtures 与测试

- Fixture 目录：`tests/fixtures/us/polygon_massive/`，synthetic OHLCV。
- 最低 fixture 集：`success.json`、`empty.json`、`missing_fields.json`、`auth_failure.json`、`rate_limited.json`、`timeout.json`、`schema_changed.json`、`duplicate_page.json`。
- 对账来源与容差：MVP 使用 synthetic golden fixture，Decimal 往返误差为 0；Phase 2 live 对账只使用已批准来源，市场价格容差按工程规范 1bp，官方宏观/利率同源重放 checksum 必须一致。
- 测试 ID：`PRV-001`～`PRV-020` applicable；`TIME-005`、`TIME-010`、`UNIT-004`。
- 在线 smoke：仅合同批准后一个 prior-day bar；默认 skip。
- 脱敏：删除 API key、account/plan details、真实 vendor payload if rights unclear。

## 运行指标与退出方案

- 告警接收人：@kazming666；授权、采购、密钥或外部 LLM 传输问题抄送 @Detachm。
- 指标：request_total/error_total/duration、records_fetched/rejected、last_success_at、data_latest_available_at、provider_stale_seconds。
- freshness：EOD 后 90 分钟目标；无合同不启用。
- 退出：停 worker、revoke key、quarantine restricted raw/cache；canonical facts 按合同/retention 决策处理。

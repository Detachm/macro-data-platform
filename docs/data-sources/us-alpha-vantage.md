# 数据源登记：Alpha Vantage

## 所有权

- Owner：@kazming666
- 区域：US / GLOBAL
- Provider role：fallback candidate only
- 数据集：daily bars / FX / news fallback candidate
- 官方文档：https://www.alphavantage.co/documentation/
- 账号负责人：@Detachm（待确认；不得提交 API key）
- 采购/合同负责人：@Detachm（commercial approval required）
- 当前状态：`fixture-only` / dev fallback；不得用于生产 ingest。
- 首次批准日期与复核日期：未批准；待 @Detachm 批准后 30 天复核，之后每季度复核。
- 关联 Issue：[#2](https://github.com/Detachm/macro-data-platform/issues/2)

## 接口与覆盖

- Base URL 与端点：`GET https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=<symbol>&outputsize=<compact|full>&datatype=json&apikey=<secret>`。
- 请求参数、分页/cursor：无 cursor；`compact/full` 控制返回窗口。
- 频率、时区和上游时间字段：daily series by exchange date；precise availability timestamp 未确认，使用 `first_seen`。
- 历史深度、修订策略：endpoint/plan-specific；未采购不承诺。
- 代码、单位、币种和空值规则：ticker 先经 alias 解析；Decimal(str(raw))。
- 限流、并发、超时和重试要求：plan-specific quota；429/limit note 不当成空数据。

## 权利矩阵

| 权利 | 允许 | 依据/到期日 |
|---|---:|---|
| storage_allowed | false | commercial approval 未确认 |
| internal_analysis_allowed | false | commercial approval 未确认 |
| external_llm_allowed | false | 未授权 |
| embedding_allowed | false | 未授权 |
| redistribution_allowed | false | 未授权 |

## 公共合同映射

MVP 不实现 Alpha Vantage live provider；仅作为 synthetic fixture shape。若后续获 commercial approval，必须仍输出公共 `MarketBar` / `MarketObservation` / `NewsEvent`，不得新增 US 私有 DTO。

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| `symbol` | `canonical_symbol` / `instrument_id` | 先经 US alias 解析；不直接信任 vendor ticker。 | 是 | quarantine `SYMBOL_UNRESOLVED` |
| time-series date | `trading_date` / `bar_start` / `bar_end` | 按 `America/New_York` 交易日；无精确发布时间时 `available_at=first_seen`。 | 是 | quarantine `TIMEZONE_REQUIRED` |
| OHLCV fields | `open/high/low/close/volume` | Decimal(str(raw))；校验 OHLC 和非负 volume。 | 是 | quarantine `INVALID_OHLC` |
| `Last Refreshed` metadata | `source.provider_updated_at` candidate | 仅作为 metadata；不能证明 PIT `available_at`。 | 否 | `null` |
| article/news fields | `NewsEvent` fields | 未获授权前禁止保存 body/summary。 | 否 | `body=null`, `summary=null` |

Identity basis：`instrument_id + interval + bar_start + adjustment + provider_id`。
`available_at` basis：默认 `first_seen`。
Checksum：canonical vendor row JSON，不含 API key、retrieved_at 或分页位置。
Source URL：endpoint URL without secret query params。
稳定排序键：bars `bar_end ASC, instrument_id ASC, bar_id ASC`；news `published_at DESC, news_id DESC`。

## 失败与降级

- Missing key / unapproved plan：health=`not_configured`，不启用 live worker。
- 401 / invalid key：`ProviderAuthenticationError`，不重试。
- 403 / unapproved plan / license denied：`ProviderAuthorizationError`，不重试。
- 429 或 quota note：`ProviderRateLimitError`，按 provider 提示退避。
- Timeout：`ProviderTimeoutError`，bounded retry；不推进 watermark。
- HTML login/auth wall/risk-control page：`ProviderAuthorizationError`，不得当成空数据。
- Malformed JSON / unexpected non-JSON payload、schema drift、字段改名：`ProviderSchemaError`。
- Empty page loop / repeated provider window：`ProviderCursorError` / `INVALID_PAGINATION` after threshold。
- Cursor expiry：无 cursor；若后续 provider 引入不透明 cursor，过期映射为 `ProviderCursorError`。
- 合法空响应：`ProviderPage(items=[], complete=True)`。

## Fixtures 与测试

- Fixture 目录：`tests/fixtures/us/alpha_vantage/`（仅 synthetic，不复制 vendor 示例正文）。
- 最低 fixture 集：`success.json`、`empty.json`、`missing_fields.json`、`auth_failure.json`、`rate_limited.json`、`timeout.json`、`schema_changed.json`、`duplicate_page.json`。
- 对账来源与容差：MVP 使用 synthetic golden fixture，Decimal 往返误差为 0；Phase 2 live 对账只使用已批准来源，市场价格容差按工程规范 1bp，官方宏观/利率同源重放 checksum 必须一致。
- 测试 ID：适用 `PRV-001`～`PRV-021`；bars 覆盖 `TIME-005`、`UNIT-004`；news 覆盖 `NEWS-012`、`NEWS-017`。
- 在线 smoke：无合同前禁止；获批后只拉一个 prior-day synthetic-approved ticker。
- 脱敏方式：删除 API key、账号、vendor messages 中任何 plan/account 信息。

## 运行指标与退出方案

- 告警接收人：@kazming666；授权、采购、密钥或外部 LLM 传输问题抄送 @Detachm。
- 指标：request count/error/duration、records fetched/rejected、last_success_at、stale_seconds、schema_validation_failure_total。
- 阈值：429 进入 retry_wait；schema drift 阻断该 adapter。
- 退出：撤销 key、停用 provider role、quarantine 新抓取数据；若误入 live 数据，按授权事件处理并清除 raw/cache。

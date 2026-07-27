# 数据源登记：Twelve Data Basic US daily bars

## 所有权

- Owner：@kazming666
- 区域：US
- Provider role：计划由 #34 绑定为 `us.market.primary`；本登记本身不注册 adapter。
- 数据集：仅 `bars`，仅 `SPY`、`QQQ`、`DIA`，仅 `1day`、`raw` OHLCV。
- 官方文档：https://twelvedata.com/docs
- 账号负责人：@Detachm 在运行时 Secret Manager 分配；本仓库不记录用户名、API key 或账户标识。
- 采购/合同负责人：@Detachm；使用 Twelve Data Basic 的个人内部使用范围，不采购或谈判本 PR 之外的授权。
- 当前状态：`approved + production_enabled`，但仅限内部 ingestion 与 canonical facts 存储。
- 首次批准日期与复核日期：2026-07-27；30 天后复核，之后每季度复核，且 Twelve Data 条款、套餐或消费路径变化时立即复核。
- 批准依据：项目负责人 [#34 comment](https://github.com/Detachm/macro-data-platform/issues/34#issuecomment-5087802577) 明确选择 Twelve Data Basic、`SPY`/`QQQ`/`DIA` 日线，并限定个人内部使用、允许存储、禁止外部 LLM。
- 关联 Issue：[#2](https://github.com/Detachm/macro-data-platform/issues/2)、[#26](https://github.com/Detachm/macro-data-platform/issues/26)、[#34](https://github.com/Detachm/macro-data-platform/issues/34)。

## 接口与覆盖

- Base URL 与端点（不得写 token）：`GET https://api.twelvedata.com/time_series`；使用 `symbol=<SPY|QQQ|DIA>`、`interval=1day`、`start_date`、`end_date`、`outputsize` 和仅来自运行时 Secret Manager 的 `apikey`。
- 请求参数、分页/cursor 语义：以单 symbol、有限日期窗口请求；该端点不提供 cursor。adapter 必须限制窗口和响应条数，并拒绝重复或倒序日期，不得把超出白名单的 symbol 请求给 provider。
- 频率、时区和上游时间字段：Twelve Data 的 `1day` 行以交易所本地交易日日期给出；按 `America/New_York` 交易日历构造 `trading_date`、session 边界和 UTC 时间。日线不把请求的 `timezone` 参数当作时间语义。
- 历史深度、更新延迟、修订策略：深度、额度和可用历史依套餐而定；日线响应没有可审计的 dissemination timestamp 时，修订行以 checksum/revision 保存，`available_at=first_seen`，绝不从交易日日期伪造发布时间。
- 代码、单位、币种和空值规则：仅允许原始 provider symbol `SPY`、`QQQ`、`DIA`；先通过 US alias/instrument 映射取得 canonical symbol。OHLCV 使用 `Decimal(str(raw))`，价格币种为 `USD`；缺任一 OHLC 或无法解析日期的行 quarantine。
- 限流、并发、超时和重试要求：遵守当前 Basic 套餐额度；限制并发、设置连接/读取超时。429 按 `Retry-After` 或 bounded exponential backoff 重试；401/403 与授权页不得重试。

## 公共合同映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| `meta.symbol` / 请求 symbol | `source_symbol`、`canonical_symbol` / `instrument_id` | 保留 provider 原始大写代码；只接受 `SPY`、`QQQ`、`DIA`，再经 US alias 解析。 | 是 | quarantine `SYMBOL_UNRESOLVED` |
| `values[].datetime` | `trading_date`、`bar_start`、`bar_end` | 解析为 `America/New_York` 本地交易日，再映射为 UTC session 边界。 | 是 | quarantine `TIMEZONE_REQUIRED` |
| `values[].open/high/low/close` | `MarketBar.open/high/low/close` | `Decimal(str(value))`；校验 `low <= open/close <= high`。 | 是 | quarantine `INVALID_OHLC` |
| `values[].volume` | `MarketBar.volume` | 非负 `Decimal(str(value))`。 | 否 | `null` + quality flag |
| response row + request metadata | `source.provider_record_id` | `<provider_id>:<ticker>:1day:<bar_start>:raw`。 | 是 | quarantine |
| platform fetch completion | `first_seen_at` / `available_at` | `available_at=first_seen_at`；无 provider dissemination proof 时不得使用交易日日期。 | 是 | quarantine |

Identity basis：`instrument_id + interval + bar_start + adjustment + provider_id`。
`available_at` basis：平台 `first_seen`。
Checksum：排序后的 canonical OHLCV JSON 加业务 source metadata；排除 `retrieved_at`、请求 API key、分页位置，保留任何 provider 修订字段。
Source URL：不含 `apikey` 的 `https://api.twelvedata.com/time_series?symbol=<ticker>&interval=1day`。
稳定排序键：`bar_end ASC, instrument_id ASC, bar_id ASC`。

## 权利矩阵

| 权利 | 允许 | 依据/到期日 |
|---|---:|---|
| storage_allowed | true | 负责人于 2026-07-27 明确批准内部存储；仅 canonical daily facts、仅白名单三个 symbol。 |
| internal_analysis_allowed | true | Twelve Data Individual/Basic 的个人或内部使用范围；仅内部 worker 与持久化。复核 2026-08-26。 |
| external_llm_allowed | false | 负责人明确禁止；策略必须在传入外部模型前拒绝。 |
| embedding_allowed | false | 未获明确批准；与外部模型边界一致，默认拒绝。 |
| redistribution_allowed | false | Individual/Basic 条款不授权向第三方再分发或商业展示；禁止作为对外报告引用。 |

## 失败与降级

- 缺少 API key：health=`not_configured`，不启动 live worker。
- 401 / invalid key：`ProviderAuthenticationError`，不重试。
- 403 / plan、地域或符号不授权：`ProviderAuthorizationError`，不重试。
- 429：`ProviderRateLimitError`；按 `Retry-After` 或 bounded backoff，且不推进 checkpoint。
- Timeout：`ProviderTimeoutError`；bounded retry，不推进 checkpoint。
- HTML login page / auth wall / risk-control page（伪 200）：`ProviderAuthorizationError`，绝不当作空页或 schema drift。
- malformed JSON / unexpected non-JSON payload、schema drift、字段改名：`ProviderSchemaError`。
- 重复日期、倒序窗口、空页循环：`ProviderCursorError` / `INVALID_PAGINATION`；达到阈值才失败。
- 无 cursor；若请求窗口与 checkpoint 不可衔接，拒绝推进并以 `ProviderCursorError` quarantine。
- 不引入未经批准的 fallback；Twelve Data 不可用时报告相应 US market 输入为缺失，不以 fixture 代替生产数据。

## Fixtures 与测试

- Fixture 目录：#34 创建 `tests/fixtures/us/twelve_data/`，仅合成/脱敏 API shape，不复制账号信息或受限原文。
- 测试 ID：#26 `GOV-026`（策略和 symbol scope）；#34 覆盖 `PRV-001`～`PRV-021` 的适用项、`TIME-005`、`PIT-009`、`UNIT-004`。
- 对账来源与容差：获批准的 Twelve Data 单 symbol 日线响应；价格与 volume 以同一 canonical response checksum 对账，Decimal 误差为 0。
- 在线 smoke 的最小请求和成本：仅一个已收盘交易日、一个白名单 symbol、`interval=1day`；仅在显式 `live` marker、运行时 key 和额度预算均满足时执行。
- 脱敏方式与正文保留限制：fixture 删除 `apikey`、账户/套餐信息和 provider 原始错误详情；只保留必要 OHLCV/日期字段，不保存新闻正文或 provider account metadata。

最低 fixture：`success.json`、`empty.json`、`missing_fields.json`、`auth_failure.json`、`rate_limited.json`、`timeout.json`、`schema_changed.json`、`duplicate_page.json`。

## 运行指标与退出方案

- freshness / completeness / rejection / latency 阈值：每个白名单 symbol 的最新交易日可用性、三标的 completeness、429/401/403/schema rejection 计数、p95 latency 和 checkpoint lag；连续一个交易日缺失或任一授权错误阻断该输入。
- 告警接收人：@kazming666；授权、套餐、凭据和外部使用告警同时通知 @Detachm。
- 数据源停用、凭据撤销、历史数据删除或保留步骤：立即停用 `us.twelve-data.v1` role、撤销 Secret Manager key、停止调度和 checkpoint；按负责人/条款决定清除或保留既有 canonical facts，并记录审计决定。任何外部 LLM、引用或再分发尝试都应先拒绝并触发授权复核。

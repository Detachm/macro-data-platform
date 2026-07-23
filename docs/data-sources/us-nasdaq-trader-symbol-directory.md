# 数据源登记：Nasdaq Trader Symbol Directory

## 所有权

- Owner：@kazming666
- 区域：US
- Provider role：`us.instruments.primary`
- 数据集：instruments
- 官方文档：https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs
- 账号负责人：@Detachm（待确认；当前无账号）
- 采购/合同负责人：@Detachm（rights review 未完成）
- 当前状态：`fixture-only`；不得宣称 live-ready。
- 首次批准日期与复核日期：未批准；待 @Detachm 批准后 30 天复核，之后每季度复核。
- 关联 Issue：[#2](https://github.com/Detachm/macro-data-platform/issues/2)

## 接口与覆盖

- Base URL 与端点：`https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt`、`https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt`。
- 请求参数、分页/cursor：全量 pipe-delimited 文件，无 cursor；按文件 checksum 增量。
- 频率、时区和上游时间字段：文件日内更新；footer 有 `File Creation Time`，timezone 未确认前仅保存 raw footer 并使用 `first_seen`。
- 历史深度、修订策略：当前目录文件；历史 alias 由平台快照和 Adds/Deletes 维护。
- 代码、单位、币种和空值规则：显式 MIC 映射；不按代码猜市场；`BRK.B` 保留点号。
- 限流、并发、超时和重试要求：官方页未给数值；worker 低频抓取、指数退避。

## 权利矩阵

| 权利 | 允许 | 依据/到期日 |
|---|---:|---|
| storage_allowed | false | 交易所 reference data rights 未复核；fixture only |
| internal_analysis_allowed | true | 仅用于合成 fixture 和 adapter 设计 |
| external_llm_allowed | false | 未授权 |
| embedding_allowed | false | 未授权 |
| redistribution_allowed | false | 未授权 |

## 公共合同映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| `Symbol` | `Instrument.local_symbol` | 大写；保留点号；不按数字猜市场。 | 是 | quarantine `SYMBOL_UNRESOLVED` |
| listing exchange / market category | `venue_mic` | 显式 MIC map，如 XNAS/XNYS；模糊则拒绝。 | 是 | quarantine `AMBIGUOUS_SYMBOL_ALIAS` |
| `Security Name` | `Instrument.name` | 去首尾空白，不做展示改写。 | 是 | quarantine |
| `ETF` | `asset_class` | `Y` → `etf`；普通股 → `equity`。 | 是 | quarantine |
| `Round Lot Size` | `lot_size` | Decimal(str(raw))。 | 否 | `null` |
| file footer creation time | `available_at` candidate | timezone 未确认前不用作 PIT；保存 raw footer。 | 否 | `first_seen` |

Identity basis：`venue_mic + local_symbol + valid_from`。
`available_at` basis：默认 `first_seen`；footer timezone 经确认后才可升级。
Checksum：canonical source row JSON，不含 retrieved_at。
Source URL：source file URL。
稳定排序键：`canonical_symbol ASC, valid_from ASC, instrument_id ASC`。

## 失败与降级

- Fetch timeout：`ProviderTimeoutError`，retry with bounded backoff；不推进 watermark。
- 401 / protected file requires auth：`ProviderAuthenticationError`，不重试。
- 403 / access denied / auth wall：`ProviderAuthorizationError`，不重试。
- 429 / rate limit：`ProviderRateLimitError`，按 header/schedule 退避。
- HTML login/auth wall/risk-control page：`ProviderAuthorizationError`，不得当成空数据。
- Malformed text / unexpected non-pipe-delimited payload：`ProviderSchemaError`。
- Empty file / missing footer：`ProviderSchemaError`，不当成 delisting。
- Empty page loop：全量文件无 cursor；若 future pagination 重复空页，`ProviderCursorError` / `INVALID_PAGINATION` after threshold。
- Cursor expiry：全量文件无 cursor；若 future cursor 过期，映射为 `ProviderCursorError`。
- Unknown exchange code：quarantine `AMBIGUOUS_SYMBOL_ALIAS`。
- Duplicate source symbol same effective date：quarantine batch `AMBIGUOUS_SYMBOL_ALIAS`。

## Fixtures 与测试

- Fixture 目录：`tests/fixtures/us/instruments/nasdaq_trader/`。
- 最低 fixture 集：`success.json`、`empty.json`、`missing_fields.json`、`auth_failure.json`、`rate_limited.json`、`timeout.json`、`schema_changed.json`、`duplicate_page.json`。
- 对账来源与容差：MVP 使用 synthetic golden fixture，Decimal 往返误差为 0；Phase 2 live 对账只使用已批准来源，市场价格容差按工程规范 1bp，官方宏观/利率同源重放 checksum 必须一致。
- 测试 ID：`SYM-004`～`SYM-010`、`SYM-012`、`PRV-001`、`PRV-002`、`PRV-020`、`PRV-021`。
- 在线 smoke：无 rights approval 前禁止保存 live snapshot；可人工 fetch 校验 shape。
- 脱敏：不包含账号、cookie、商业 reference data dump。

## 运行指标与退出方案

- 告警接收人：@kazming666；授权、采购、密钥或外部 LLM 传输问题抄送 @Detachm。
- 指标：request_total/error_total/duration、records_fetched/rejected、last_success_at、unresolved_symbol_count。
- freshness：文件日内未更新标 stale，不伪造 delisting。
- 退出：停用 source config；保留平台 alias history，raw snapshots 按 rights review 删除或留存。

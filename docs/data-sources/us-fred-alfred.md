# 数据源登记：FRED / ALFRED

## 所有权

- Owner：@kazming666
- 区域：US / GLOBAL
- Provider role：none for MVP ingest; discovery/manual cross-check only
- 数据集：macro/rates discovery reference only
- 官方文档：https://fred.stlouisfed.org/docs/api/terms_of_use.html
- 账号负责人：@Detachm（API key 不配置到 MVP ingest）
- 采购/合同负责人：@Detachm（legal/project approval required for any ingest）
- 当前状态：非 ingest；在逐 series owner rights review 和负责人批准完成前，不得持久化 FRED-derived observations。
- 首次批准日期与复核日期：未批准；待 @Detachm 批准后 30 天复核，之后每季度复核。
- 关联 Issue：[#2](https://github.com/Detachm/macro-data-platform/issues/2)

## 接口与覆盖

- Base URL 与端点：`https://api.stlouisfed.org/fred/...`；仅作人工研究参考。
- 请求参数、分页/cursor：`api_key`、`series_id`、`limit`、`offset`、`realtime_start/end` 等；MVP adapter 不实现。
- 频率、时区和上游时间字段：FRED release dates 不等于 FRED availability；不能当 PIT `available_at`。
- 历史深度、修订策略：series-specific；ALFRED vintage 可供研究。
- 代码、单位、币种和空值规则：series-specific；未进入公共 ingest。
- 限流、并发、超时和重试要求：不适用；不配置 live worker。

## 权利矩阵

| 权利 | 允许 | 依据/到期日 |
|---|---:|---|
| storage_allowed | false | 平台 no-ingest 策略；待逐 series owner rights review 和负责人批准。FRED terms 要求遵守 data owner restrictions。 |
| internal_analysis_allowed | false | 平台非 ingest 默认值；待批准。 |
| external_llm_allowed | false | 平台 no-LLM/no-embedding 策略；待批准。 |
| embedding_allowed | false | 平台 no-LLM/no-embedding 策略；待批准。 |
| redistribution_allowed | false | 未完成逐 series 权利复核。 |

## 公共合同映射

MVP 不实现 FRED/ALFRED ingest provider。以下仅定义未来若项目负责人和授权复核批准后的约束。

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| `series_id` | `MacroSeries.series_id` candidate | 只能使用批准 allowlist；不能污染公共 taxonomy。 | 是 | quarantine |
| `date` | `period_start` / `period_end` | 日期级 observation，不代表 availability。 | 是 | quarantine |
| `value` | `MacroObservation.value` | Decimal(str(raw)); `.` 或 missing → null。 | 否 | `null` + quality flag |
| `realtime_start/end` | `vintage_id` metadata | 仅作为 vintage 日期；不能单独证明 intraday PIT。 | 是 | quarantine |
| series notes/rights | `usage/right config` | rights 未明确时全部 false。 | 是 | disabled |

Identity basis：未来若批准，使用 `series_id + period_end + vintage_id + provider_id`。
`available_at` basis：默认不适用；批准后仍必须使用平台 `first_seen` 或 agency timestamp，不用 FRED release date 伪造。
Checksum：canonical approved series observation JSON，不含 API key。
Source URL：FRED endpoint without API key。
稳定排序键：`period_end ASC, series_id ASC, available_at ASC`。

## 失败与降级

- 默认：health=`not_configured`，无 worker。
- 任何未批准 live call：视为 policy violation，停止并打开 follow-up。
- 401 / invalid key：未来获批 provider 中映射为 `ProviderAuthenticationError`。
- 403 / license denied / auth wall / risk-control：未来获批 provider 中映射为 `ProviderAuthorizationError`。
- 429 / rate limit：未来获批 provider 中映射为 `ProviderRateLimitError`。
- Timeout：未来获批 provider 中映射为 `ProviderTimeoutError`。
- HTML login/auth wall/risk-control page：`ProviderAuthorizationError`，不得当成空数据。
- Malformed JSON / unexpected non-JSON provider payload、schema drift：`ProviderSchemaError`。
- Empty page loop / repeated offset window：`ProviderCursorError` / `INVALID_PAGINATION` after threshold。
- Cursor expiry：FRED/ALFRED offset-style pagination 没有平台 cursor；若 adapter 引入 cursor，过期映射为 `ProviderCursorError`。

## Fixtures 与测试

- Fixture 目录：`tests/fixtures/us/fred_alfred/`（仅用于 non-ingest policy tests）。
- 最低 fixture 集：`success.json`、`empty.json`、`missing_fields.json`、`auth_failure.json`、`rate_limited.json`、`timeout.json`、`schema_changed.json`、`duplicate_page.json`。
- 对账来源与容差：MVP 使用 synthetic golden fixture，Decimal 往返误差为 0；Phase 2 live 对账只使用已批准来源，市场价格容差按工程规范 1bp，官方宏观/利率同源重放 checksum 必须一致。
- 测试 ID：policy test 确认 FRED provider 未注册；PIT test 确认不返回 FRED-derived observations。
- 在线 smoke：禁止，除非新审批 Issue 明确允许。
- 脱敏：不提交 API key、不提交 series notes 中受限文本长摘录。

## 运行指标与退出方案

- 告警接收人：@kazming666；授权、采购、密钥或外部 LLM 传输问题抄送 @Detachm。
- 指标：无 live metrics；若误启用，记录 policy_violation_total。
- 退出：停用 role、quarantine/purge raw FRED payload、重新生成受影响 context，并记录 incident。

# 数据源登记：Federal Reserve H.10/H.15 Data Download

## 所有权

- Owner：@kazming666
- 区域：US / GLOBAL
- Provider role：`us.rates_fx.primary`
- 数据集：market_observations
- 官方文档：https://www.federalreserve.gov/datadownload/
- 账号负责人：@Detachm（无 API key）
- 采购/合同负责人：@Detachm（public statistical releases）
- 当前状态：official numeric data `live-candidate`；MVP provider 仍先 fixture-backed。
- 首次批准日期与复核日期：未批准；待 @Detachm 批准后 30 天复核，之后每季度复核。
- 关联 Issue：[#2](https://github.com/Detachm/macro-data-platform/issues/2)

## 接口与覆盖

- Base URL 与端点：Federal Reserve Data Download Program generated CSV/XML packages；H.10/H.15 current release pages。
- 请求参数、分页/cursor：package/download URL 固定在 source config；无 cursor。
- 频率、时区和上游时间字段：release/page metadata if available；否则 observation date + platform `first_seen`。
- 历史深度、修订策略：package-specific；保存 source release/date metadata。
- 代码、单位、币种和空值规则：percent 保持 percent value；FX/index 使用 `rate` 或 `index_point`。
- 限流、并发、超时和重试要求：官方未给稳定数值；低频计划任务 + backoff。

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
| release package / series code | `metric_code` | H.10 FX/index、H.15 rates 映射到公共 metric taxonomy。 | 是 | quarantine `METRIC_UNRESOLVED` |
| observation date | `period_start` / `period_end` / `observed_at` | 日期级观测；不伪造发布时间。 | 是 | quarantine |
| value | `MarketObservation.value` | Decimal(str(raw)); percent 保持 percent，FX 使用 rate。 | 否 | `null` + quality flag |
| unit metadata | `unit` / `currency` | `percent`、`rate`、`index_point`。 | 是 | quarantine `UNIT_REQUIRED` |
| release metadata | `available_at` / `availability_basis` | 有官方发布时间则 provider_disseminated；否则 `first_seen`。 | 是 | quarantine |

Identity basis：`region + scope_type + scope_id + metric_code + observed_at + provider_id`。
Checksum：canonical Fed package row JSON，不含 retrieved_at。
Source URL：configured DDP package/current release URL。
稳定排序键：`observed_at ASC, observation_id ASC`。

## 失败与降级

- Download unavailable/timeout：retry with bounded backoff；coverage stale。
- HTML login/auth wall/risk-control page：`ProviderAuthorizationError`。
- Malformed CSV/JSON or unexpected non-data payload：`ProviderSchemaError`。
- Schema/package column changed：schema drift warning，阻断 adapter。
- 合法缺值：`value=null` + quality flag，不写 0。

## Fixtures 与测试

- Fixture 目录：`tests/fixtures/us/federal_reserve/`。
- 最低 fixture 集：`success.json`、`empty.json`、`missing_fields.json`、`auth_failure.json`、`rate_limited.json`、`timeout.json`、`schema_changed.json`、`duplicate_page.json`。
- 对账来源与容差：MVP 使用 synthetic golden fixture，Decimal 往返误差为 0；Phase 2 live 对账只使用已批准来源，市场价格容差按工程规范 1bp，官方宏观/利率同源重放 checksum 必须一致。
- 测试 ID：`PRV-001`～`PRV-020` applicable；`UNIT-001`、`UNIT-002`、`UNIT-005`、PIT available_at tests。
- 在线 smoke：下载一个 latest/current package metadata；低频、无 aggressive polling。

## 运行指标与退出方案

- 告警接收人：@kazming666；授权、采购、密钥或外部 LLM 传输问题抄送 @Detachm。
- 指标：request_total/error_total/duration、records_fetched/rejected、last_success_at、data_latest_available_at、provider_stale_seconds。
- freshness：按 release cadence；无新数据时 stale，不造值。
- 退出：停用 package config；public canonical facts 保留，raw/cache 按 retention 清理。

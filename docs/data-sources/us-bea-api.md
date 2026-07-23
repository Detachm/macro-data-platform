# 数据源登记：BEA API

## 所有权

- Owner：@kazming666
- 区域：US
- Provider role：`us.macro.primary`
- 数据集：macro_series / macro_observations / macro_releases
- 官方文档：https://apps.bea.gov/api/signup/
- 账号负责人：@Detachm（UserID 待配置；不得提交）
- 采购/合同负责人：@Detachm（public statistics）
- 当前状态：official numeric data `live-candidate`；MVP provider 仍先 fixture-backed。
- 首次批准日期与复核日期：未批准；待 @Detachm 批准后 30 天复核，之后每季度复核。
- 关联 Issue：[#2](https://github.com/Detachm/macro-data-platform/issues/2)

## 接口与覆盖

- Base URL 与端点：`GET https://apps.bea.gov/api/data`。
- 请求参数、分页/cursor：`UserID`、`method=GetData`、`DataSetName`、dataset-specific params、`ResultFormat=JSON`；按 dataset params/window 切分。
- 频率、时区和上游时间字段：BEA release schedule；precise timezone from machine-readable feed must be confirmed before exact `scheduled_at` use。
- 历史深度、修订策略：dataset-specific；GDP/PCE revisions 新增 vintage，不覆盖旧值。
- 代码、单位、币种和空值规则：table/line → series ID；unit/transformation 分 series。
- 限流、并发、超时和重试要求：100 requests/min、100 MB/min、30 errors/min；429 with `Retry-After`。

## 权利矩阵

| 权利 | 允许 | 依据/到期日 |
|---|---:|---|
| storage_allowed | true | official public statistics |
| internal_analysis_allowed | true | official public statistics |
| external_llm_allowed | true | numeric facts with source citation |
| embedding_allowed | true | numeric facts |
| redistribution_allowed | true | notice/no-endorsement controls |

## 公共合同映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| `DataSetName` + table/line/code | `MacroSeries.series_id` / `code` | `macro:US:BEA:<CODE>`；level、qoq、yoy、annualized 分 series。 | 是 | quarantine `SERIES_UNRESOLVED` |
| period fields | `period_start` / `period_end` | 按 BEA period semantics；不伪造具体发布时间。 | 是 | quarantine |
| value | `MacroObservation.value` | Decimal(str(raw)); missing token → `null` + quality flag。 | 否 | `null` |
| unit / line metadata | `unit` / `transformation` | 按 dataset metadata 固定。 | 是 | quarantine `UNIT_REQUIRED` |
| release schedule | `MacroRelease.scheduled_at` | timezone 未确认前不得作为精确 PIT evidence。 | 否 | `released_at=null` |
| revision metadata | `vintage_id` / `revision_no` | 每次修订新增 vintage，不覆盖。 | 是 | quarantine |

Identity basis：`series_id + period_end + vintage_id + provider_id`；release 使用 `series_id + scheduled_at + period_end + provider_id`。
`available_at` basis：无 API dissemination proof 时用平台 `first_seen`。
Checksum：canonical BEA response row + dataset metadata JSON，不含 UserID、retrieved_at。
Source URL：`https://apps.bea.gov/api/data` with secret params removed。
稳定排序键：observations `period_end ASC, series_id ASC, available_at ASC`；releases `scheduled_at ASC, release_id ASC`。

## 失败与降级

- Missing `UserID`：health=`not_configured`；fixture provider 可继续。
- 401/403 或 invalid key response：auth/authorization error，不重试。
- 429 with `Retry-After`：`ProviderRateLimitError`。
- 30 errors/min 触发：circuit open，停止当前 run。
- Dataset schema/metadata changed：`ProviderSchemaError`；不批量写 null。
- 合法无 observation：空 page + warning，不报错。

## Fixtures 与测试

- Fixture 目录：`tests/fixtures/us/bea/`。
- 最低 fixture 集：`success.json`、`empty.json`、`missing_fields.json`、`auth_failure.json`、`rate_limited.json`、`timeout.json`、`schema_changed.json`、`duplicate_page.json`。
- 对账来源与容差：MVP 使用 synthetic golden fixture，Decimal 往返误差为 0；Phase 2 live 对账只使用已批准来源，市场价格容差按工程规范 1bp，官方宏观/利率同源重放 checksum 必须一致。
- 最低 fixture：success、empty、missing_fields、auth_failure、rate_limited、timeout、schema_changed、duplicate_page。
- 测试 ID：`PRV-001`～`PRV-020` applicable；macro PIT `PIT-001`、`PIT-002`、`PIT-006`。
- 在线 smoke：`GetDatasetList` + 一个小 NIPA window；缺 UserID 自动 skip。
- 脱敏：移除 UserID、request URL secret query。

## 运行指标与退出方案

- 告警接收人：@kazming666；授权、采购、密钥或外部 LLM 传输问题抄送 @Detachm。
- 指标：request_total/error_total/duration、records_fetched/rejected、last_success_at、data_latest_available_at、schema_validation_failure_total。
- freshness：按 BEA release schedule；迟到标 stale，不造值。
- 退出：撤销 UserID、停用 role；保留 public canonical facts，raw/cache 按授权复核处理。

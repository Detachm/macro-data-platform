# 数据源登记：NewsAPI / licensed media placeholder

## 所有权

- Owner：@kazming666
- 区域：US / GLOBAL
- Provider role：`us.news.primary` licensed candidate
- 数据集：news
- 官方文档：https://newsapi.org/docs
- 账号负责人：@Detachm（待采购；不得提交 API key）
- 采购/合同负责人：@Detachm（licensed media contract required）
- 当前状态：`fixture-only`；无合同前不得 live ingest。
- 首次批准日期与复核日期：未批准；待 @Detachm 批准后 30 天复核，之后每季度复核。
- 关联 Issue：[#2](https://github.com/Detachm/macro-data-platform/issues/2)

## 接口与覆盖

- Base URL 与端点：`GET https://newsapi.org/v2/everything` after procurement only。
- 请求参数、分页/cursor：`q`、`from`、`to`、`language`、`sortBy`、`pageSize`、`page`；page/pageSize pagination。
- 频率、时区和上游时间字段：`publishedAt` documented as UTC；provider first-seen unavailable，use platform `first_seen`。
- 历史深度、修订策略：plan-specific；developer/free plan not production.
- 代码、单位、币种和空值规则：headline/snippet only when licensed；body default `null`。
- 限流、并发、超时和重试要求：plan-specific quota；429 must not be treated as empty data。

## 权利矩阵

| 权利 | 允许 | 依据/到期日 |
|---|---:|---|
| storage_allowed | false | contract required |
| internal_analysis_allowed | false | contract required |
| external_llm_allowed | false | publisher content rights not approved |
| embedding_allowed | false | 未授权 |
| redistribution_allowed | false | 未授权 |

## 公共合同映射

MVP 不启用 live NewsAPI ingest；仅 synthetic fixture。若后续合同批准，默认仍禁止 body，summary/snippet 需合同逐项允许。

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| `url` / provider article ID | `news_id` / `source.provider_record_id` | canonical URL hash or provider ID。 | 是 | quarantine `NEWS_IDENTITY_MISSING` |
| `title` | `title` | 保留语义；规范化只用于 dedup。 | 是 | quarantine |
| `description` / `content` | `summary` / `body` | 无合同前全部 `null`；合同后按 rights allowlist。 | 否 | `null` |
| `publishedAt` | `published_at` | UTC timestamp；`available_at` 仍用平台 `first_seen`。 | 是 | quarantine |
| `source.name` | `source_name` / `source_tier` | 默认 `licensed_media`。 | 是 | quarantine |
| `url` | `canonical_url` / `source.source_url` | 去 tracking 参数。 | 是 | quarantine |

Identity basis：provider ID if present else canonical URL + published_at。
Checksum：canonical headline/source/timestamp metadata JSON，不含 restricted text、API key、retrieved_at。
Source URL：canonical article URL and API endpoint without key。
稳定排序键：`published_at DESC, news_id DESC`。

## 失败与降级

- Missing contract/key：health=`not_configured`。
- Developer/free plan in production：`ProviderAuthorizationError`。
- 401/403：auth/authorization error，不重试。
- 429：`ProviderRateLimitError`。
- `articles` schema drift / truncated content shape change：`ProviderSchemaError`。
- Body/snippet not allowed：quarantine or redact to `null` before `NewsEvent`。

## Fixtures 与测试

- Fixture 目录：`tests/fixtures/us/newsapi/`，synthetic only。
- 最低 fixture 集：`success.json`、`empty.json`、`missing_fields.json`、`auth_failure.json`、`rate_limited.json`、`timeout.json`、`schema_changed.json`、`duplicate_page.json`。
- 对账来源与容差：MVP 使用 synthetic golden fixture，Decimal 往返误差为 0；Phase 2 live 对账只使用已批准来源，市场价格容差按工程规范 1bp，官方宏观/利率同源重放 checksum 必须一致。
- 测试 ID：`NEWS-002`、`NEWS-003`、`NEWS-012`、`NEWS-013`、`NEWS-017`、`PRV-007`～`PRV-010`。
- 在线 smoke：禁止，直到 contract approves storage/internal use。
- 脱敏：不提交 API key、vendor examples、real publisher body。

## 运行指标与退出方案

- 告警接收人：@kazming666；授权、采购、密钥或外部 LLM 传输问题抄送 @Detachm。
- 指标：request_total/error_total/duration、records_fetched/rejected、news_duplicate_ratio、schema_validation_failure_total。
- freshness：provider delay/plan-specific；无合同不评估。
- 退出：停 role、purge restricted raw/cache、重建 context fingerprint。

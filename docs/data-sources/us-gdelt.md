# 数据源登记：GDELT news discovery

## 所有权

- Owner：@kazming666
- 区域：GLOBAL / US-filtered
- Provider role：`us.news.primary` candidate for metadata discovery
- 数据集：news metadata only
- 官方文档：https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
- 账号负责人：@Detachm（无 API key）
- 采购/合同负责人：@Detachm（underlying publisher rights review required）
- 当前状态：`fixture-only` / disabled until approval；不抓取、不保存 publisher body。
- 首次批准日期与复核日期：未批准；待 @Detachm 批准后 30 天复核，之后每季度复核。
- 关联 Issue：[#2](https://github.com/Detachm/macro-data-platform/issues/2)

## 接口与覆盖

- Base URL 与端点：`GET https://api.gdeltproject.org/api/v2/doc/doc`。
- 请求参数、分页/cursor：`query`、`mode=artlist`、`maxrecords`、`timespan`、`format=json`；无稳定 cursor。
- 频率、时区和上游时间字段：保存 provider raw timestamps；timezone 不明确时使用 platform `first_seen`。
- 历史深度、修订策略：query/window-specific；不作为 authoritative publisher archive。
- 代码、单位、币种和空值规则：URL/title/source metadata only；no body。
- 限流、并发、超时和重试要求：hosted APIs rate limited；官方未确认固定全局数值；低频窗口抓取。

## 权利矩阵

| 权利 | 允许 | 依据/到期日 |
|---|---:|---|
| storage_allowed | false | pending rights review；权限不明确按不允许 |
| internal_analysis_allowed | false | pending rights review；仅 synthetic fixture |
| external_llm_allowed | false | publisher text/headline rights require review |
| embedding_allowed | false | publisher text rights require review |
| redistribution_allowed | false | publisher content rights source-specific |

## 公共合同映射

MVP 不启用 live GDELT ingest；只允许 synthetic fixture 模拟 metadata-only response。若后续批准，仅保存 URL/source/time/topic metadata，不保存 publisher body。

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| article URL / GDELT identifier | `news_id` / `source.provider_record_id` | 优先 stable ID；否则 canonical URL hash。 | 是 | quarantine `NEWS_IDENTITY_MISSING` |
| title | `title` | 标题规范化只用于 dedup；不得删除否定词。 | 是 | quarantine |
| source/time fields | `source_name` / `published_at` | timezone 不明确时不得伪造；`available_at=first_seen`。 | 是 | quarantine or date precision |
| snippet/summary | `summary` | live 未批准前必须 `null`。 | 否 | `null` |
| body | `body` | 永远不由 GDELT adapter 抓 publisher body。 | 否 | `null` |
| URL metadata | `canonical_url` / `source.source_url` | 去 tracking 参数，保留 provenance。 | 否 | `null` + flag |

Identity basis：canonical URL/content metadata hash + published_at。
Checksum：canonical metadata JSON，不含 retrieved_at，不含 publisher body。
Source URL：GDELT API URL without query secrets（无 key）和 canonical publisher URL。
稳定排序键：`published_at DESC, news_id DESC`。

## 失败与降级

- 默认 disabled：health=`not_configured`。
- 429/rate limit：`ProviderRateLimitError`；不扩大窗口。
- HTML login/auth wall/risk-control page：`ProviderAuthorizationError`。
- Malformed JSON / unexpected non-JSON payload、schema drift：`ProviderSchemaError`。
- Publisher content detected in body：quarantine `LICENSE_RESTRICTION`。

## Fixtures 与测试

- Fixture 目录：`tests/fixtures/us/gdelt/`，仅 synthetic metadata。
- 最低 fixture 集：`success.json`、`empty.json`、`missing_fields.json`、`auth_failure.json`、`rate_limited.json`、`timeout.json`、`schema_changed.json`、`duplicate_page.json`。
- 对账来源与容差：MVP 使用 synthetic golden fixture，Decimal 往返误差为 0；Phase 2 live 对账只使用已批准来源，市场价格容差按工程规范 1bp，官方宏观/利率同源重放 checksum 必须一致。
- 测试 ID：`NEWS-002`、`NEWS-003`、`NEWS-012`、`NEWS-017`；PIT `available_at <= as_of`。
- 在线 smoke：禁止，直到 rights review 明确批准 metadata storage。
- 脱敏：不保存 publisher body、restricted snippets、个人信息。

## 运行指标与退出方案

- 告警接收人：@kazming666；授权、采购、密钥或外部 LLM 传输问题抄送 @Detachm。
- 指标：若未来启用，request/error/duration、records_rejected、news_duplicate_ratio、schema_validation_failure_total。
- 退出：停 role、删除 raw/cache publisher metadata if required、重新生成受影响 context。

# 数据源登记：<provider / product / endpoint>

## 所有权

- Owner：
- 区域：CN / HK / US / GLOBAL
- Provider role：如 `cn.news.primary`
- 数据集：instruments / bars / market_observations / macro / news
- 官方文档：
- 采购/合同负责人：
- 首次批准日期与复核日期：

## 接口与覆盖

- Base URL 与端点（不得写 token）：
- 请求参数、分页/cursor 语义：
- 频率、时区和上游时间字段：
- 历史深度、更新延迟、修订策略：
- 代码、单位、币种和空值规则：
- 限流、并发、超时和重试要求：

## 公共合同映射

| 上游字段 | 公共字段 | 变换/口径 | 必填 | 缺失策略 |
|---|---|---|---:|---|
| | | | | |

明确记录 identity basis、`available_at` basis、checksum、source URL 和稳定排序键。

## 权利矩阵

| 权利 | 允许 | 依据/到期日 |
|---|---:|---|
| storage_allowed | | |
| internal_analysis_allowed | | |
| external_llm_allowed | | |
| embedding_allowed | | |
| redistribution_allowed | | |

## 失败与降级

列出 401、403、429、超时、登录页伪 200、schema drift、空页循环和 cursor 过期的错误映射及 fallback。说明禁止重试的错误。

## Fixtures 与测试

- Fixture 目录：
- 测试 ID：
- 对账来源与容差：
- 在线 smoke 的最小请求和成本：
- 脱敏方式与正文保留限制：

最低 fixture：`success.json`、`empty.json`、`missing_fields.json`、`auth_failure.json`、`rate_limited.json`、`timeout.json`、`schema_changed.json`、`duplicate_page.json`。

## 运行指标与退出方案

- freshness / completeness / rejection / latency 阈值：
- 告警接收人：
- 数据源停用、凭据撤销、历史数据删除或保留步骤：

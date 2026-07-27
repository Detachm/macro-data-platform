# ADR 0005：内部个人使用不执行来源权利 gate

- 状态：accepted
- 日期：2026-07-27

## 背景

当前系统为单一内部个人工作流，既不提供商业服务，也不对第三方分发数据。原生产策略同时限制采集、保留、EditorContext、外部 LLM 和报告引用，导致尚未审批的数据即使已经在内部仓库中也无法用于内部分析，且产生多层重复判断。

## 决定

- 删除机器可读的 production source policy 及其生产强制装载。
- 删除 JobRunner 的 ingestion/retention gate。
- 删除 EditorContext 的 production/external-LLM/citation 三联过滤。
- 删除 NewsService 基于权利标记的正文和摘要清洗。
- v1 `UsageRights` 等字段暂时保留为兼容元数据，但不参与运行时判断。
- 凭据防泄漏、服务认证、PIT、来源追踪、质量和幂等规则不变。

本 ADR supersede ADR 0001 中关于平台 no-ingest/no-LLM 默认策略的运行时部分；其数据源研究、PIT 和身份规则继续有效。

## 后果

内部工作流可以使用仓库中所有数据。若未来改为多人、商业、公开或第三方分发部署，需要重新设计与部署形态匹配的集中式出站策略，而不是恢复散落在采集、服务和报告各层的布尔 gate。

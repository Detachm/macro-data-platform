# 内部个人使用边界

本项目当前只服务单一内部个人工作流，不提供商业服务、第三方数据分发或公共访问。

## 运行时行为

- 数据采集不再依赖来源审批清单，也不按 retention rule 拒绝写入。
- `EditorContext` 不再按 production、external LLM 或 citation 标记过滤事实。
- 新闻标题、摘要和正文不再根据 `usage_rights` 降级或删除。
- 报告和内部 LLM 可以使用规范化仓库中已有的全部事实与内容。
- `UsageRights` 和 provider capability 中的旧权利字段仅为 v1 payload 兼容元数据，运行时不读取这些字段作准入判断；在下一个破坏性 API 版本中可以删除。

## 仍然保留的边界

- API 服务令牌、provider key、Cookie、账号、密码等凭据不得进入 Git、日志、fixture、报告或 LLM prompt。
- `available_at <= as_of`、来源追踪、checksum、幂等、质量状态和失败可观测性继续执行。
- provider 的技术能力、配额、限速和可用性仍由 adapter 负责；移除权利 gate 不会自动增加尚未实现的 live adapter。

来源登记文件中原有的 rights matrix 保留为历史研究和未来部署形态变化时的参考，不再构成当前内部个人运行时的阻断条件。

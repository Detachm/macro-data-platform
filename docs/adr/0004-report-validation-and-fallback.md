# ADR 0004：日报事实校验与安全降级

- 状态：proposed
- 日期：2026-07-27
- 关联 Issue：[#31](https://github.com/Detachm/macro-data-platform/issues/31)

## 决策

- 发布前使用 `ReportValidator` 校验 `DailyReport` draft；事实、来源溯源、`available_at`、报告日期和机器可读 claim 都必须能回溯到同一个不可变 `ReportInputSnapshot`。
- 必需输入由显式 quality gate 记录驱动。缺失、过期、迟到、不可用、隔离或无效的必需输入都会阻断发布，并把输入 ID 和原因写入报告质量信息及 `validation_errors`。
- LLM 没有返回可验证 draft 时，`ReportFallbackBuilder` 只从已批准 snapshot 的 display value 构造确定性模板；必需事实不完整时生成 `incomplete/not_published` 版本，不使用旧数据或未经批准的内容补齐。
- 校验结果通过报告版本的原子状态更新保存。失败版本保留为 `failed/not_published`，人工重新生成必须使用新的 report ID/version，不覆盖被拒绝版本。

## 范围边界

- 本 ADR 不改变 provider 的采集、质量 gate 的判定算法或 LLM prompt；它消费已经持久化的 snapshot 和 quality gate 结果。
- `validation_errors` 是报告持久化审计字段，使用 forward migration 增加，不删除既有报告数据。

## 回滚

停止调用校验/降级服务并保留已写入的报告版本与错误记录。数据库回滚使用对应 migration 的 downgrade；不得删除失败版本来隐藏审计记录。

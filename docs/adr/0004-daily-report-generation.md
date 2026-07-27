# ADR 0004：可审计日报生成边界

- 状态：partially superseded by ADR 0005
- 日期：2026-07-27
- 关联 Issue：[#30](https://github.com/Detachm/macro-data-platform/issues/30)

## 决策

- 报告生成器只接受已持久化的 `ReportInputSnapshot`，不持有 provider、registry 或数据库事实查询依赖。
- 生成输入通过固定 `daily-report-v1.0` prompt preset 进入 `LlmClient`；LLM 只返回结构化报告 draft。
- 每次生成记录独立的 generation attempt，保存 prompt version、model、参数、输入 fingerprint、source reference IDs、attempt 次数和错误码。
- `draft`、`generated`、`failed`、`validated`、`superseded` 是 generation attempt 和已落库报告版本的生命周期状态；最终 `DailyReport` payload 仍按 #25 的不可变报告版本保存。
- LLM 超时和结构化输出错误执行有界重试；失败 attempt 保留，不能用空报告伪装成功。
- prompt 不保存原始 prompt 文本；输入来自持久化 EditorContext，token、Cookie、账号、密码和其他凭据始终拒绝进入 prompt；正文可以用于内部个人工作流。

## 范围边界

- 本 ADR 不实现 provider、事实质量校验、fallback 发布决策或 Feishu 交付。
- 事实是否存在于 snapshot、是否满足 freshness 和是否可以发布由 #31 校验。
- LLM vendor authentication、实际网络客户端和运行时 secret 管理由部署层提供；本 Issue 只定义可替换的 client seam。

## 回滚

停止调用 report generator 并保留已写入的 snapshot、generation attempt 和 report payload。需要撤销 schema 时使用 forward migration，不删除已有审计记录。

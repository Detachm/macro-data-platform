# ADR 0009：日期锁定的采集 worker 与输入质量门禁

- 状态：accepted
- 日期：2026-07-27
- 关联 Issue：[#29](https://github.com/Detachm/macro-data-platform/issues/29)

## 背景

日报输入来自多个可独立失败的地区和数据集。API 不得临时访问上游，且同一报告日不能由两个
worker 并行编排。已有 checkpointed `JobRunner` 负责单个 provider 请求的持久化 run、幂等写入和
watermark 恢复，但还缺少报告日期级的排他执行、可重试任务编排和可审计的输入质量结果。

ADR 0005 已决定内部个人使用不设置运行时来源权利 gate。因此本决策只能评估完整性、时效、
隔离、修订和可重试状态，不能把 `usage_rights`、`citation_allowed` 或历史 `denied` 标记作为
采集、报告或 LLM 的准入条件。

## 候选方案

1. API 进程中按请求触发抓取。实现简单，但违反 API/worker 进程隔离，且无法可靠恢复。
2. worker 仅以进程内锁串行执行。实现成本低，但重启或多副本部署时不能保证排他性。
3. 独立 worker 使用 PostgreSQL session advisory lock，并把实际 provider 操作委托给既有
   checkpointed `JobRunner`。

## 决策

- 采用方案 3。`PostgresReportDateLock` 对稳定散列后的报告日期调用
  `pg_try_advisory_lock(bigint)`；锁连接在整组任务完成前保持，其他 worker 得到明确的 `locked`
  终态，不会重复执行。
- `ScheduledIngestionWorker` 按任务执行有限指数退避重试，并产出
  `succeeded`、`degraded`、`blocked`、`retryable` 或 `locked`。必需任务失败阻断，必需任务用尽
  短暂错误预算返回 `retryable`，可选失败降级。
- 手动 backfill 是显式、闭区间的 report-date 循环，复用相同日期锁和任务幂等边界。每个实际
  provider 任务仍必须经 `JobRunner`，从其持久化 checkpoint/watermark 恢复，不能靠 scheduler
  内存状态宣称恢复。
- `ReportInputQualityGate` 从不可变 input snapshot 生成质量结论：缺失、过期、迟到、不可用、
  隔离和无效的必需输入 `blocked`；必需的暂时错误为 `retryable`；修订与可选问题为 `degraded`。
  input snapshot 没有 materialized facts 时同样 `blocked`。历史 `denied` 标记被忽略；它只可能是
  旧来源权利元数据，绝不构成质量或来源权利规则。
- 本 PR 故意让 `build_registered_tasks()` 返回空元组。报告日历、各地区任务到 input snapshot 的
  materialization、以及生产 cron/触发时间尚未有冻结合同；在它们定义前不得登记 provider 或发起
  live ingestion。空 task bundle 若被直接执行会 fail closed 为 `blocked`；该扩展点是范围边界，
  不是已完成的生产调度。
- `macro-data-worker` 入口在注册为空时记录 `SCHEDULER_NOT_CONFIGURED` 并以失败状态退出，不会以
  空闲进程伪装成正常生产 worker。

## 后果与回滚

- 日志记录 report date、任务、provider role、attempt、run ID 和终态；指标记录报告与任务终态。
- advisory lock 只保护报告日编排，不能替代事实表的唯一约束或 provider run fencing。
- 若 worker 行为异常，停用任务注册或停止 worker 进程即可；不删除已持久化的事实、run 或
  checkpoint。恢复时由每个 task 的 `JobRunner` 重放安全页。
- 自动计划和 snapshot materializer 启用前，必须另开 Issue/ADR 并补真实 PostgreSQL e2e，证明连续
  两个报告日期、重启恢复和 API 只读路径。

## 验证

- `RPT-029`：必需隔离和缺失输入阻断、可选修订降级、必需 retryable 输入不发布。
- `JOB-029`：限次重试、date lock 冲突、闭区间 backfill。
- PostgreSQL integration：两个独立连接竞争同一 report date，第二个 worker 不得取得锁。

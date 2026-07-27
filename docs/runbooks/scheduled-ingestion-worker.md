# Scheduled ingestion worker

`macro-data-worker` 是 API 之外的独立进程。它提供 report-date advisory lock、有限重试、
backfill 和质量门禁的公共执行边界；API 继续只读取已入库的数据。

## 当前范围

- `ScheduledIngestionWorker.run_for_date(report_date)` 在 PostgreSQL advisory lock 内运行一组任务。
  已被其他 worker 持有时返回 `locked`，不会等待后再重复抓取。
- `backfill(start_date, end_date)` 逐个、包含首尾日期执行，且每一天复用相同的锁与任务幂等边界。
- 必需任务失败为 `blocked`；用尽可重试预算为 `retryable`；可选任务失败为 `degraded`。
- `ReportInputQualityGate` 只检查完整性、时效、隔离、修订与临时错误。来源权利、引用权限和
  external-LLM 标记不是运行时 gate；历史 `denied` 标记被忽略；见 ADR 0005。

## 有意留空的生产注册

本阶段 `build_registered_tasks()` 返回空元组。尚未实现下列依赖，因此 `macro-data-worker` 不能被
当作已经可运行的生产采集计划：

- 报告日历到任务窗口的配置合同；
- provider task 到 `ReportInputSnapshot.input_quality` 和事实 materialization 的映射；
- 自动触发频率与运行时部署配置；
- 跨两个真实报告日的 PostgreSQL e2e。

后续实现必须把每个 provider task 包装为 checkpointed `JobRunner`，将其 run ID、checkpoint 和
quarantine evidence 写入 input snapshot；不得直接在 scheduler 内访问上游，也不得用内存 cursor
充当恢复证据。

直接执行空 task bundle 会得到 `blocked`，而不是成功或降级；这样未完成的生产注册不会被质量状态
误报为可发布。`macro-data-worker` 入口发现空注册会记录 `SCHEDULER_NOT_CONFIGURED` 后失败退出，
不会常驻空转。

## 观测与故障处理

日志事件 `scheduled_task_finished` 含 `report_date`、`task_id`、`run_id`、`provider_role`、
`dataset`、`region`、`attempt_no`、`duration_ms`、`record_count`、`terminal` 和 `error_code`。
Prometheus 指标为
`scheduled_report_run_total` 与 `scheduled_task_run_total`。

遇到 `locked` 时确认另一个 worker 是否仍健康；不要解除其他进程的 PostgreSQL session lock。
遇到 `retryable` 时依 provider run/checkpoint 检查上游错误和下一次重试；遇到 `blocked` 时先修复
必需输入的完整性、时效或 quarantine 原因，再执行同一 report date 的显式 backfill。

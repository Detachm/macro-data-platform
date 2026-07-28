# Scheduled ingestion worker

`macro-data-worker` 是 API 之外的独立进程。它提供 report-date advisory lock、有限重试、
checkpoint 恢复、backfill 和质量门禁的公共执行边界；API 继续只读取已入库的数据。

## 当前范围

- `ScheduledIngestionWorker.run_for_date(report_date)` 在 PostgreSQL advisory lock 内运行一组任务，
  随后 materialize 不可变 `ReportInputSnapshot`。已被其他 worker 持有时返回 `locked`，不会等待后
  再重复抓取。checkpoint 使用 lease epoch/owner CAS；被重新认领前的 worker 无法再推进 cursor 或计数。
- `backfill(start_date, end_date)` 逐个、包含首尾日期执行，且每一天复用相同的锁与任务幂等边界。
- 必需任务失败为 `blocked`；用尽可重试预算为 `retryable`；可选任务失败为 `degraded`。
- `ReportInputQualityGate` 只检查完整性、时效、隔离、修订与临时错误。来源权利、引用权限和
  external-LLM 标记不是运行时 gate；历史 `denied` 标记被忽略；见 ADR 0005。

## 已注册任务与运行方式

- 生产仅在 `PROVIDER_MODE=live` 时注册 `cn.daily-bars`、`hk.daily-bars`、`us.daily-bars`、
  `cn.macro-release-calendar`、`hk.official-headlines`；它们分别调用 BaoStock、XtQuant、Twelve
  Data、NBS、HKMA 的 checkpointed handler。fixture role 不会注册。
- 每个 task 的 `(report_date, task_id)` checkpoint 保存原始 request clock 和 next cursor。进程在
  page commit 后退出时，下一 worker 从该 cursor 继续；完成 task 复放其 durable run result，不重新
  访问 provider。normalized facts 仍由 page commit 和事实表唯一约束去重。
- 默认上海 07:50 开始，08:15 为 input cutoff。通过 `WORKER_SCHEDULE_*`、
  `WORKER_REPORT_CUTOFF_*`、`WORKER_*_FRESHNESS_*` 配置时区、时刻、轮询和时效阈值。
- 常规常驻：`macro-data-worker`。
- 单日演练：`macro-data-worker --report-date 2026-07-28`。
- 显式回填：`macro-data-worker --backfill-start 2026-07-20 --backfill-end 2026-07-28`。首尾日期均包含，
  不可与单日模式混用。也可使用对应 `WORKER_RUN_ONCE_REPORT_DATE` 或 `WORKER_BACKFILL_*` 环境变量。

CN news 和 US macro calendar 尚无获批的 live provider。materializer 若不能从事实库得到它们的合格
数据，会将相关必需输入写为 `missing`，报告质量为 `blocked`；这是预期的安全状态，直到其各自的
provider Issue 实现完成。不可使用 fixture 或其他未登记来源填充这些输入。

## 观测与故障处理

日志事件 `scheduled_task_finished` 含 `report_date`、`task_id`、`run_id`、`provider_role`、
`dataset`、`region`、`attempt_no`、`duration_ms`、`record_count`、`terminal` 和 `error_code`。
Prometheus 指标为
`scheduled_report_run_total` 与 `scheduled_task_run_total`。

遇到 `locked` 时确认另一个 worker 是否仍健康；不要解除其他进程的 PostgreSQL session lock。
遇到 `retryable` 时依 provider run/checkpoint 检查上游错误和下一次重试；遇到 `blocked` 时先修复
必需输入的完整性、时效或 quarantine 原因，再执行同一 report date 的显式 backfill。不要以 rights、
引用或 external-LLM 元数据作为解除阻断的条件。

报告生成时会再次检查已选择的 input snapshot。若质量为 `blocked` 或 `retryable`，系统仅记录
`REPORT_INPUT_QUALITY_*` generation attempt，不会构建 prompt 或调用 LLM。

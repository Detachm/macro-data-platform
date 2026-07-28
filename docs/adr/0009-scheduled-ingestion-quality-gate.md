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
  input snapshot 没有 materialized facts、或 payload facts 与声明的 `fact_ids` 不一致时同样
  `blocked`。历史 `denied` 标记被忽略；它只可能是旧来源权利元数据，绝不构成质量或来源权利规则。
- 成功的 scheduled task 必须返回其 durable `run_id`；缺失时 worker 改记为
  `MISSING_DURABLE_RUN_ID` 并 fail closed。所有 live task 都经 checkpointed `JobRunner` 执行。
- 已注册的 live 任务为 `cn.daily-bars`（BaoStock 三个 CN 核心指数）、`hk.daily-bars`（XtQuant
  审核的十个 HK 标的）和 `us.daily-bars`（Twelve Data 的 SPY/QQQ/DIA）。每个请求取报告日上海
  当地午夜之前、可配置的 14 天回看窗口，因而只请求上一交易日及更早的日线。
- 新增 `scheduled_task_checkpoints`。其主键是 `(report_date, task_id)`，保存稳定的 live request
  clock、下一页 opaque cursor、source watermark、最后的 durable run ID 与接受/隔离计数。page
  commit 仍是事实写入的唯一幂等边界；task checkpoint 只负责在 worker 重启后从已提交页继续。完成的
  task 直接复放该持久化结果，不再访问上游。
- `ReportInputSnapshotMaterializer` 从 PostgreSQL 规范化事实、revision、`ingest_rejections` 和
  task 终态派生 `facts`、`source_references` 与 `input_quality`，随后写入不可变 snapshot。默认
  上海 07:50 抓取、08:15 截止；晚于 cutoff 到达的事实为 `late`，超过可配置窗口的事实为 `stale`。
  CN/HK news 和 CN/US macro release 尚无 live checkpointed handler，因此在没有已入库事实时显式
  标为 `missing`，阻止报告，而不是伪造成功。
- `macro-data-worker` 默认在可配置的时区、时刻和轮询间隔下每天运行一次。运维可使用
  `macro-data-worker --report-date YYYY-MM-DD` 或
  `macro-data-worker --backfill-start YYYY-MM-DD --backfill-end YYYY-MM-DD`；两种模式互斥，backfill
  是包含首尾日期的串行循环并复用同一 advisory lock 与 checkpoint。

## 后果与回滚

- 日志记录 report date、任务、provider role、attempt、run ID 和终态；指标记录报告与任务终态。
- advisory lock 只保护报告日编排，不能替代事实表的唯一约束或 provider run fencing。
- 若 worker 行为异常，停用任务注册或停止 worker 进程即可；不删除已持久化的事实、run 或
  checkpoint。恢复时由每个 task 的 `JobRunner` 重放安全页。
- 无法绑定任一 reviewed live provider role 时，worker 仍以 `SCHEDULER_NOT_CONFIGURED` 失败退出；
  fixture provider 不能作为生产 fallback。

## 验证

- `RPT-029`：必需隔离和缺失输入阻断、可选修订降级、必需 retryable 输入不发布；snapshot 不读取或
  写入任何 rights/LLM runtime gate。
- `JOB-029`：限次重试、date lock 冲突、闭区间 backfill、完成 task 的 durable replay。
- PostgreSQL e2e：两个独立连接竞争同一 report date；worker 在首个 page commit 后重启，从下一页
  cursor 恢复，连续两个 backfill report date 均写入不可变 input snapshot，且 normalized bars 无重复。

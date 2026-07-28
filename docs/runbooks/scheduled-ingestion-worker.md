# Scheduled ingestion worker

`macro-data-worker` 是 API 之外的独立进程。一个服务完成采集、质量门禁、报告生成、校验和
飞书投递；report-date advisory lock 覆盖整条链路。API 继续只读取已入库的数据。

## 当前范围

- `ScheduledIngestionWorker.run_for_date(report_date)` 在 PostgreSQL advisory lock 内运行一组任务，
  随后 materialize 不可变 `ReportInputSnapshot`。已被其他 worker 持有时返回 `locked`，不会等待后
  再重复抓取。checkpoint 使用 lease epoch/owner CAS；被重新认领前的 worker 无法再推进 cursor 或计数。
- `backfill(start_date, end_date)` 逐个、包含首尾日期执行，且每一天复用相同的锁与任务幂等边界。
- 必需任务失败为 `blocked`；用尽可重试预算为 `retryable`；可选任务失败为 `degraded`。
- `08:15` 冻结的快照通过门禁后生成报告；未配置真实 LLM 时使用经过事实校验的确定性 fallback，
  并把本次结果标记为 `degraded`。正常日报最早在 `08:30` 发到日报群。
- `blocked`、重试预算耗尽、生成/校验失败和日报投递失败会向独立预警群发送持久化、幂等的红色
  告警。飞书结果为 `uncertain` 时禁止自动重发。
- `ReportInputQualityGate` 只检查完整性、时效、隔离、修订与临时错误。来源权利、引用权限和
  external-LLM 标记不是运行时 gate；历史 `denied` 标记被忽略；见 ADR 0005。

## 已注册任务与运行方式

- 生产仅在 `PROVIDER_MODE=live` 时注册 `cn.daily-bars`、可选的 `hk.daily-bars`、`us.daily-bars`、
  `cn.macro-release-calendar`、`hk.official-headlines`；它们分别调用 BaoStock、XtQuant、Twelve
  Data、NBS、HKMA 的 checkpointed handler。fixture role 不会注册。
- 每个 task 的 `(report_date, task_id)` checkpoint 保存原始 request clock 和 next cursor。进程在
  page commit 后退出时，下一 worker 从该 cursor 继续；完成 task 复放其 durable run result，不重新
  访问 provider。normalized facts 仍由 page commit 和事实表唯一约束去重。XtQuant 的 HK 任务只采集
  审核过的十个个股，不能作为冻结的 `market.hk.core_indices.previous_close` 质量证据。
- 默认上海 07:50 开始，08:15 为 input cutoff。通过 `WORKER_SCHEDULE_*`、
  `WORKER_REPORT_CUTOFF_*`、`WORKER_MAX_REPORT_ATTEMPTS`、市场/新闻 `WORKER_*_FRESHNESS_*` 配置时区、时刻、
  报告级重试上限、轮询和时效阈值。采集开始时刻必须早于 cutoff；provider 任务在 07:50 发起，若
  提前完成则 materializer 等到 08:15 再冻结 snapshot。首次见到时间晚于 cutoff 的事实标记为 `late`。
- 市场日线 freshness 使用 XSHG、XHKG、XNYS 的交易日历来确定上一交易会话，不以工作日近似；日历
  无法覆盖的报告日会以 `unavailable` 阻断报告，待日历版本更新后再重跑。
- 常规常驻：`macro-data-worker`。
- 单日演练：`macro-data-worker --report-date 2026-07-28`。
- 修复生成或校验终态失败后，需要人工核对审计记录，再使用新的不可变版本执行：
  `macro-data-worker --report-date 2026-07-28 --report-version v2-reviewed`。同一日期、同一版本的
  普通重放是幂等的；不要为了重试随意改版本。
- 显式回填：`macro-data-worker --backfill-start 2026-07-20 --backfill-end 2026-07-28`。首尾日期均包含，
  不可与单日模式混用。也可使用对应 `WORKER_RUN_ONCE_REPORT_DATE` 或 `WORKER_BACKFILL_*` 环境变量。

HK 核心指数、CN news 和冻结的全区域 `calendar.macro_releases_7d` 尚不能证明完整获批 live 覆盖。materializer
将相应必需输入写为 `missing`，报告质量为 `blocked`；历史行、fixture 或其他未登记来源也不能填充
这些输入。CN NBS 日历只覆盖 CN，不能单独满足该全区域输入。这是预期的安全状态，直到各地区
provider Issue 实现完成。

## 观测与故障处理

日志事件 `scheduled_task_finished` 含 `service`、`report_date`、`task_id`、`run_id`、`provider_role`、
`dataset`、`region`、`attempt_no`、`duration_ms`、`record_count`、`terminal` 和 `error_code`。
`scheduled_report_finished` 另含 `workflow_run_id`、`snapshot_id`、`report_id`、`quality_status`、
`delivery_status`、`alert_status` 和 `terminal_stage`。Prometheus 指标为
`scheduled_report_run_total` 与 `scheduled_task_run_total`。

遇到 `locked` 时确认另一个 worker 是否仍健康；不要解除其他进程的 PostgreSQL session lock。
遇到 `retryable` 时依 provider run/checkpoint 检查上游错误和下一次重试；常驻调度对同一报告日最多
执行 `WORKER_MAX_REPORT_ATTEMPTS` 次（默认 3，包含首次）。`blocked` 是该常驻进程中的终态：先修复
必需输入的完整性、时效或 quarantine 原因，再执行同一 report date 的显式 backfill。不要以 rights、
引用或 external-LLM 元数据作为解除阻断的条件。

报告生成时会再次检查已选择的 input snapshot。若质量为 `blocked` 或 `retryable`，系统仅记录
`REPORT_INPUT_QUALITY_*` generation attempt，不会构建 prompt 或调用 LLM。

## 生产配置与恢复边界

生产环境必须同时设置 `PROVIDER_MODE=live`、`FEISHU_DELIVERY_ENABLED=true`、飞书应用凭据、日报群
`FEISHU_CHAT_ID` 和预警群 `FEISHU_ALERT_CHAT_ID`；缺失完整投递工作流时进程拒绝启动。日常版本由
`REPORT_WORKFLOW_VERSION=v1` 固定，生成模型、超时和尝试次数分别由 `REPORT_GENERATION_MODEL`、
`REPORT_GENERATION_TIMEOUT_SECONDS`、`REPORT_GENERATION_MAX_ATTEMPTS` 设置。Secret 只进入 K8s Secret，
不得写入 Git、日志或命令参数。

恢复顺序：先按 `workflow_run_id` 核对 provider run、snapshot、generation attempt、delivery attempt 和
alert attempt；再修复上游。采集/质量失败可重放同一版本，生成/校验失败使用新的显式版本。投递失败
按下述受保护入口恢复。`uncertain` 必须先人工核群，不能直接重发。回滚应用版本不得回滚
数据库迁移；部署前按 #49 的 K8s 部署运行手册备份并验证 PostgreSQL 可恢复。

## 受保护的状态与投递恢复

三个运维入口都要求 `Authorization: Bearer <service token>`，不会返回事实值、卡片内容、Chat ID、
飞书响应或 `message_id`：

- `GET /v1/operations/worker-readiness`：检查 live provider 模式、US provider 凭据、两个不同的
  飞书群、数据库连通和 `0013` 所需关键表。全部满足返回 `200`，否则返回 `503` 和稳定的 unmet
  requirement codes；它不代替 #50/#51 的真实 provider health/coverage 检查。
- `GET /v1/operations/daily-workflows/2026-07-28`：返回 task、snapshot 质量、generation、report、
  delivery、alert 和 operator action 的脱敏状态链。
- `POST /v1/operations/daily-reports/{report_id}/delivery-retry`：只恢复既有 delivery attempt，不重新
  生成报告。

投递恢复必须由值班人员生成一个 UUID 作为 `X-Request-ID`，网络重试时复用同一个 UUID；数据库以该
ID 永久幂等。明确 `failed` 可提交 `{"confirmed_not_delivered": false}`。`uncertain` 必须先在日报群
确认消息不存在，再提交 `{"confirmed_not_delivered": true}`。`pending` 不可抢占，`succeeded` 只记录
幂等 operator action 而不会再次发消息。每次授权、拒绝、失败和成功均写入
`delivery_operator_actions`；迁移版本为 `0013`。
自动重试和累计投递次数分别受 `FEISHU_DELIVERY_MAX_ATTEMPTS`（默认 3）与
`FEISHU_DELIVERY_MAX_TOTAL_ATTEMPTS`（默认 10）限制，达到累计上限后必须先排查，不能继续换请求 ID。

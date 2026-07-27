# ADR 0002：日报 PostgreSQL 存储与幂等边界

- 状态：proposed
- 日期：2026-07-27
- 决策人：@kazming666（提议），@Detachm（待批准）
- 关联 Issue / PR：[Issue #27](https://github.com/Detachm/macro-data-platform/issues/27)

## 背景

#25 已冻结 `DailyReport` v1 的报告日期、输入快照和不可变重新生成语义。现有数据库仅有规范化事实与 ingestion checkpoint 的基础表，API 在 production 默认仍会使用空 repository，无法持久化报告输入、报告版本或交付尝试。

工程规范要求 schema、checkpoint、revision 与 PIT 策略变更具备 migration、真实 PostgreSQL 测试和明确的回滚边界。

## 候选方案

1. 将整份报告、输入和交付状态全部塞入一个 JSONB 表。
   - 拒绝：无法为报告日期/版本和交付目标建立明确唯一约束，也不利于恢复和运营查询。
2. 为每个报告 section、每个事实和每个 delivery payload 建独立关系表。
   - 暂不采用：生成器与交付器尚未实现，过早固定 section 内部结构会与 #30/#31/#32 耦合。
3. 采用不可变报告 input snapshot、报告版本和 delivery attempt 三个关系表；保留完整 JSON payload，并以 typed repository 提供幂等写入与恢复读取。
   - 采用：满足 #25 的 immutable identity、#27 的事务与恢复要求，又不预设后续生成/交付实现。

## 决策

- 新增一个 Alembic migration，仅新增 `report_input_snapshots`、`daily_reports`、`delivery_attempts` 和 ingestion run 的幂等字段；不修改已进入 main 的 migration。
- `report_input_snapshots` 以 snapshot ID 为主键，并以 report date、snapshot version、fingerprint 建唯一约束。
- `daily_reports` 以 report ID 为主键，并以 `(report_date, report_version)` 保证同一版本只能写入一次；重新生成必须使用新的 report version 和 report ID。
- `delivery_attempts` 以 `(report_id, delivery_target, idempotency_key)` 去重。失败重试更新同一 attempt，不覆盖报告。
- 正式 production app 默认注入 PostgreSQL read repository；fixture/empty repository 只在明确传入或非 production 环境使用。
- 所有事实、snapshot、report 和 delivery repository 都在调用方提供的 SQLAlchemy transaction 内工作；provider 不直接写数据库。

## 后果

- 报告、输入和交付可跨 worker 重启恢复，且重复 ingest、报告生成或交付不会产生重复记录。
- JSON payload 保留 #25 的完整报告和 source/PIT/rights 审计字段；后续若需要 section 级检索，可在兼容 migration 中增量拆表。
- 本 ADR 不实现 LLM、质量 gate 或 Feishu API；这些分别属于 #30/#31/#32。

回滚方案：在尚未有 production 数据时可回滚本 migration；进入共享环境后仅停止新写入并新增 forward migration，保留既有报告审计链。

## 验证

- `DB-001`、`DB-002`：空库和 0002 数据库均可升级到新 head。
- `DB-004`、`DB-007`：ingestion/page checkpoint 与 normalized fact 在同一事务中幂等提交。
- `REP-027-001`：相同 snapshot、report version 和 delivery key 重放不新增记录。
- `REP-027-002`：新 session 可读取已提交 checkpoint 与不可变 report。
- 检查命令：`ruff format --check .`、`ruff check .`、`mypy --strict src`、`pytest -m "not live" -q`。
- 验收人：@Nouzee 交叉评审 migration/repository；@Detachm 最终批准。

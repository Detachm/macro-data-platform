# ADR 0010：确定性的报告日与区域休市政策

- 状态：accepted
- 日期：2026-07-28
- 关联 Issue：[#51](https://github.com/Detachm/macro-data-platform/issues/51)

## 背景

日报每天 08:30（Asia/Shanghai）发布。固定把三地市场输入都视为必需，会在周末或区域交易所节假日把
正常休市误判为数据缺失；直接按星期判断又会漏掉春节、香港公众假期、美国独立日等区域差异。#33
需要消费一个明确结果，而不是让 scheduler、质量门禁、生成器分别判断日历。

## 决策

- 新增 `ExchangeReportDayPolicy`，使用固定映射 XSHG/CN、XHKG/HK、XNYS/US 解析报告日。
- 每次 materialize（物化）输入快照时只计算一次，并把 policy ID、`exchange-calendars` 版本、报告日
  类型、三地 session 状态、上一交易日、required/optional 输入集合固化进不可变 snapshot 和
  `editor_context`。
- 周末三地市场输入均为 optional；工作日只把明确处于交易所节假日的地区市场输入设为 optional。
  CN/HK 官方新闻和 CN/HK/US 宏观发布日历始终 required，不能因休市降级。
- 休市日缺少当前 market input 变为 `unavailable` optional issue，报告为 `degraded` 但可继续；正常
  交易日的缺失、过期、迟到或无效 market input 仍然 `blocked` 并触发预警。
- quality gate（质量门禁）验证 policy 的报告日、集合互斥和完整分区；伪造、缺字段或试图降级新闻/
  日历时全部输入恢复为 required，并以 `REPORT_DAY_POLICY_INVALID` fail closed（安全失败）。
- 交易所日历覆盖不了目标日期时不猜测，materializer 失败，由既有 worker/预警流程处理。

## 边界

本 ADR 持久化的是每份报告实际采用的 session 决策，不把第三方 Python 包当成 CN 新闻或 HK/US
宏观发布日历 provider。#51 仍需接入获批官方来源、保存每条事实的 `available_at` 与 provenance，
并完成连续两个报告日 live 验收。

## 后果与回滚

- 旧 snapshot 没有 `report_day_policy` 时继续按 v1 固定 required 集合校验，避免历史数据语义变化。
- 升级 `exchange-calendars` 会改变新 snapshot 的 `calendar_version`；升级前必须跑冻结的周末和区域
  节假日 contract tests，并人工核对未来假期。
- 如策略异常，回滚本变更后旧固定门禁会恢复；已保存 snapshot 保持不可变。

## 验证

- 普通工作日三地 market inputs 均 required。
- 周末三地 market inputs 均 optional，新闻和日历仍 required。
- 2026-07-03 只把 US 市场识别为 exchange holiday，CN/HK 仍 required。
- 日历解析失败或 snapshot policy 被篡改时 fail closed。

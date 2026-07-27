# ADR 0007：BaoStock CN 核心指数日线

- 状态：accepted
- 日期：2026-07-27
- 关联 Issue：[#28](https://github.com/Detachm/macro-data-platform/issues/28)

## 背景

#28 需要真实 CN/HK 日线以替代只能验证 parser 的 synthetic fixture。项目负责人选择 BaoStock
作为不需要账号或 token 的 CN 公开来源。它的 Python client 是 stateful 的同步会话，且可查询的
标的范围不应被实现时的便利性扩大为任意证券。

## 决定

- 注册 `cn.baostock.v1` 为 live `cn.bars.primary`，仅支持 `bars / CN / 1d / raw`。
- 静态 allowlist 仅包括 SSE Composite (`sh.000001`)、CSI 300 (`sh.000300`) 和 Shenzhen
  Component (`sz.399001`)；它们分别映射为 `XSHG:000001`、`XSHG:000300` 和 `XSHE:399001`。
- 每次查询在 worker thread 内以串行的 login/query/logout session 执行；上游 fields、日期窗口、
  row count 和 symbol 均为严格 contract。分页由 adapter 的签名 cursor 和 source checksum 提供。
- 上游没有可验证的历史发布快照，因此 raw bar 的 `available_at` 为平台抓取时间，
  `availability_basis=first_seen`，历史 PIT 请求拒绝。
- 通过 checkpointed ingestion 保存 instrument、bar、raw-time audit、quarantine evidence 和
  page watermark；不保存 API 凭据或 BaoStock 原始响应。
- HK 日线、CN 个股和其他指数不在本 ADR 范围内；#28 不因本项被视为全部完成。

## 验证与回滚

- 使用 mock client 的 provider contract、错误映射、cursor/snapshot、checkpoint 持久化和可选
  live smoke 验证；live smoke 只查询一条允许的核心指数。
- 回滚时解绑 `cn.bars.primary`、停用 BaoStock job；保留已有规范化记录及其来源审计，除非项目负责人
  要求删除。

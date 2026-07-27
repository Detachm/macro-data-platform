# ADR 0006：CN/HK live provider 的日期精度与 allowlist contract

- 状态：accepted
- 日期：2026-07-27

## 背景

Issue #28 接入 CN NBS release calendar、HK C&SD `510-60004` 和 HKMA press-release
metadata。上游有些记录只有日期，没有可证明的发布时间；C&SD 的 series metadata 也必须
由显式 registry 审核，不能按上游字段推断。

## 决定

- 公共 `MacroRelease` 和 `NewsEvent` 使用 `time_precision` 区分 `instant` 与 `date`；日期精度记录使用 `scheduled_date` / `published_date`，不伪造午夜 timestamp。
- CN/HK fixture parser 与 live adapter 使用相同的日期精度 contract，并在分页 cursor 中绑定 query、snapshot watermark、snapshot timestamp 和排序前驱。
- C&SD 只接受代码 registry 中登记的 series、描述和频率；metadata drift 隔离为 `ProviderSchemaError`。
- live fixture 仅保存合成或脱敏的公开响应样例；不包含凭据、Cookie 或受限正文。
- `UsageRights` 保留为兼容溯源元数据，不参与内部个人使用工作流的运行时发布拦截。

## 影响与验证

- 这是向 v1 公共 contract 添加可选字段的兼容变化；同步更新工程规范、数据源映射、fixture parser 和 provider contract tests。
- 验证包括 CN/HK live provider contract、date-only fixture replay、C&SD metadata drift、分页 snapshot binding、ruff、mypy 和单一 migration head。

## 回滚

回滚 #28 provider 注册、fixture parser 和对应 contract tests；保留已发布 contract 字段，不原位恢复旧的时间语义。

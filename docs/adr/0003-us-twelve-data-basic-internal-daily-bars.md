# ADR 0003：Twelve Data Basic 的 US 内部日线范围

- 状态：accepted
- 日期：2026-07-27
- 决策人：@Detachm
- 关联 Issue / PR：[Issue #26](https://github.com/Detachm/macro-data-platform/issues/26)、[Issue #34](https://github.com/Detachm/macro-data-platform/issues/34)

## 背景

ADR 0001 将 US 日线行情保留为 fixture-only：Polygon/Massive 需要商业授权，Alpha Vantage 仅为未获批的原型候选。Issue #34 需要在 #26 allowlist 批准后，以 live provider 覆盖报告所需的 US market 输入。

项目负责人于 2026-07-27 明确选择 Twelve Data Basic，首批仅接入 `SPY`、`QQQ`、`DIA` 日线；使用场景是个人内部使用、允许存储、禁止外部 LLM。Twelve Data 的个人套餐也不应被当作第三方再分发或商业展示授权，因此需要把范围和禁止项放入可执行的生产策略，而不能只写在 issue comment。

## 候选方案

1. 保持 Polygon/Massive 为 US market primary。
   - 拒绝：现有 matrix 已记录需要商业市场数据授权，当前没有批准的合同。
2. 采用 Twelve Data Basic，但对所有可查询 symbol、外部 LLM 与报告引用开放。
   - 拒绝：负责人只批准三只 market proxy；个人/Basic 使用范围不构成外部模型、引用或再分发授权。
3. 采用 Twelve Data Basic，仅为 `SPY`、`QQQ`、`DIA` 的 `1day raw` bars 开放内部 ingestion 与 canonical facts 存储。
   - 采用：满足最小 US market proxy 范围，并以 machine-readable policy 限制 provider、dataset、region 和 symbols。

## 决策

- `us.twelve-data.v1 / bars / US` 是目前唯一 `approved + production_enabled` 的生产数据策略条目。
- 允许范围只包括 `SPY`、`QQQ`、`DIA` 的日线原始 OHLCV；`allowed_symbols` 由 policy 决定，#34 adapter 必须只请求、输出并持久化这三个 provider symbol。
- 使用 API key 的方式必须是运行时 Secret Manager；key、账号和套餐标识不得进入仓库、fixture 或日志。
- 允许内部 ingestion 和 `canonical_facts` 保存。`available_at` 没有可靠 provider dissemination proof 时使用平台 `first_seen`。
- 禁止外部 LLM、embedding、报告引用及再分发。生产 `EditorContext` 的政策校验必须因此拒绝该来源进入任何需要外部 LLM 或 citation 的消费路径。
- 对其他 Twelve Data dataset、其他 US symbol、intraday、调整行情或任何放宽外部使用边界，都需要新的负责人审批与 policy/ADR 变更。

## 后果

- #34 可以实现一个受 policy symbol scope 约束的 live daily-bars adapter，并保留 fixture adapter 仅用于确定性测试。
- 该批准不等价于对外报告数据授权。若 #25 的最终报告会向第三方展示 US market 数据，必须先获得适用的 display/redistribution 授权，再修改 `citation_allowed` / `external_llm_allowed`。
- 错误配置为额外 symbol、外部 LLM 或 citation 时，生产策略会拒绝相应消费；adapter 仍必须在请求前执行 symbol scope。
- 当套餐、额度或条款变化时，先停用 policy role 和 runtime key，再复核历史 canonical facts 的保留/删除要求。

## 验证

- `GOV-026`：打包策略对 `us.twelve-data.v1 / bars / US` 允许 ingestion 与 `canonical_facts`，返回精确 symbol scope，并拒绝 external LLM 与 citation。
- #34：live adapter 对 `SPY`、`QQQ`、`DIA` 运行共享 provider contract、错误映射、PIT、checksum 和 two-date report-input 验收；adapter 必须验证任何 scope 外 symbol 都不请求 provider。
- 检查命令：`.venv/bin/ruff format --check .`、`.venv/bin/ruff check .`、`.venv/bin/mypy --strict src`、`.venv/bin/pytest -m "not live" -q`。
- 验收人：@Detachm。

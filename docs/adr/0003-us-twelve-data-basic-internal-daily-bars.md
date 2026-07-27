# ADR 0003：Twelve Data Basic 的 US 内部日线范围

- 状态：partially superseded by ADR 0005
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

- `us.twelve-data.v1 / bars / US` 曾是来源策略的唯一生产条目；ADR 0005 已删除该运行时策略。
- `SPY`、`QQQ`、`DIA` 的日线原始 OHLCV 仍是首批 adapter 的推荐范围，但不再由 runtime policy 限制。
- 使用 API key 的方式必须是运行时 Secret Manager；key、账号和套餐标识不得进入仓库、fixture 或日志。
- 内部 ingestion 和 `canonical_facts` 保存遵循通用存储规则；`available_at` 没有可靠 provider dissemination proof 时使用平台 `first_seen`。
- 日线 base record 的首次 `available_at` 与 payload 不可被后续重抓覆盖；checksum 变化时追加 `market_bar_revisions`，其 `available_at` 为该修订版本的首次见到时间。PIT 查询只选择 `available_at <= as_of` 的最新版本。
- 外部 LLM、embedding、报告引用和再分发标记仅保留为历史元数据，不再阻断内部个人工作流。
- 其他 Twelve Data dataset、其他 US symbol、intraday 或调整行情仍需要相应 adapter 实现与测试。

## 后果

- #34 可以实现 live daily-bars adapter，并保留 fixture adapter 仅用于确定性测试。
- 本 ADR 不再定义报告或 LLM 的 runtime 准入；未来若改变部署形态，以 ADR 0005 的集中式策略原则重新设计。
- adapter 可自行约束请求 symbol，避免超出已实现和已测试的范围。
- 当套餐、额度或条款变化时，按 provider 的技术错误、配额和凭据流程处理。

## 验证

- #34：live adapter 对 `SPY`、`QQQ`、`DIA` 运行共享 provider contract、错误映射、PIT、checksum 和 two-date report-input 验收。
- 检查命令：`.venv/bin/ruff format --check .`、`.venv/bin/ruff check .`、`.venv/bin/mypy --strict src`、`.venv/bin/pytest -m "not live" -q`。
- 验收人：@Detachm。

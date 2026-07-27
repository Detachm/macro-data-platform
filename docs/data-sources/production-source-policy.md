# 生产数据与外部 LLM 来源准入策略

Issue: [#26](https://github.com/Detachm/macro-data-platform/issues/26)  
证据矩阵：[CN/HK #1](https://github.com/Detachm/macro-data-platform/issues/1)、[US #2](https://github.com/Detachm/macro-data-platform/issues/2)

机器可读的权威策略文件是
[`src/macro_platform/governance/production_source_policy.json`](../../src/macro_platform/governance/production_source_policy.json)。
它是生产接入、EditorContext、外部 LLM 和未来报告引用校验的唯一来源；这里不复制
每个来源的授权结论。

## 当前结论

截至策略版本 `2026-07-27`，唯一处于 `approved + production_enabled` 状态的条目是
`us.twelve-data.v1 / bars / US`。项目负责人已将 Twelve Data Basic 的生产范围批准为
仅内部存储 `SPY`、`QQQ`、`DIA` 的日线事实；它必须使用运行时 API key，不能发送到外部
LLM、不能作为对外报告引用，也不能再分发。完整授权、端点和退出约束见
[`us-twelve-data.md`](us-twelve-data.md) 与
[`ADR 0003`](../adr/0003-us-twelve-data-basic-internal-daily-bars.md)。

除该受限条目外，生产环境仍会拒绝所有尚未完成审批的来源，即使其技术状态是
`live-ready`。这是刻意的双重门槛，不能以在线 smoke 或无凭据公开端点替代授权审批。

已明确为 `fixture-only`、`gap`、未采购市场数据、GDELT、FRED/ALFRED 和未签约新闻来源
在策略中是 `denied`；它们不能进入生产 ingestion、EditorContext 或外部 LLM 输入。

## 条目格式与判定

每个 `provider_id + dataset + region` 条目必须有：

- `owner`、`credential_requirement`：后者是受限枚举，只记录凭据/合同类别，禁止记录 token、Cookie、账号、URL query secret 或任何实际凭据值。
- `ingestion_allowed`、`external_llm_allowed`、`citation_allowed`、`retention_rule`：分别服务于 worker、LLM 和报告生成校验。
- `approval_status`：只能是 `approved`、`pending` 或 `denied`。
- `production_enabled`：只有它、`approval_status=approved` 和 `ingestion_allowed=true` 同时成立时，生产 ingestion/EditorContext 才会放行。
- `allowed_symbols`：可选的 provider 代码白名单。存在时，live adapter 必须只请求和输出其中的代码；它不扩大 `provider_id + dataset + region` 之外的权限。
- `evidence`：必须指向 #1/#2 冻结矩阵中的具体来源登记文件或章节。

缺少条目、区域不匹配、`pending` 或 `denied` 一律拒绝。策略只返回可审计的决定和原因，永不读取或暴露凭据。

运行时接缝：

- `ProductionSourcePolicy.decision(...)` / `require(...)` 是 worker 和报告校验使用的稳定接口。
- `JobRunner(..., source_policy=...)` 是必填依赖；生产策略会在调用 handler 前同时检查 ingestion 与 retention，未获准的来源不能进入写入路径。
- 生产 `EditorContext` 对 market、macro、news 所有记录同时检查 `editor_context`、`external_llm` 和 `citation` 决定；无来源的 snapshot 默认拒绝。
- 生产 ingestion 会把每个 region 的 `retention_rule` 传给记录写入 handler；`metadata_only` handler 不得写入 canonical facts。
- 开发与 fixture 测试使用不执行生产准入的 policy，避免将 fixture 合同误认为生产授权。

## 新来源变更流程

新增 provider、dataset 或 region 前，必须在同一个可评审 PR 中修改 JSON 策略，并同时：

1. 先补齐或更新 #1/#2 风格的数据来源登记与 `evidence`。
2. 明确 owner、凭据/合同前提、四类使用权限和 retention rule；未知权限填 `pending`/`denied`，不得猜测为允许。
3. 只有项目负责人完成审批后，才将 `approval_status` 改为 `approved` 并将需要生产调度的数据集设为 `production_enabled=true`。
4. 添加 policy 单测，以及涵盖真实 provider_id 的 ingestion 或 EditorContext 回归测试。

`metadata_only` 只允许保存 contracts 中的 metadata/规范化事实；新闻正文仍以来源登记中的
`允许保存正文` 为准。当前没有 production news 写入 handler，后续实现该 handler 时必须在
写入前调用 `PolicyPurpose.RETENTION`，并拒绝不允许保存正文的 payload。

策略模型会拒绝 `production_enabled=true` 但未获审批或不允许 ingestion 的条目；重复的
`provider_id + dataset + region` 也会在加载时失败。

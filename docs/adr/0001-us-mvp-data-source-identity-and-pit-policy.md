# ADR 0001：US MVP data sources, identity/checksum, and PIT policy

- 状态：partially superseded by ADR 0005
- 日期：2026-07-23
- 决策人：@kazming666（提议），@Detachm（待批准）
- 关联 Issue / PR：[Issue #2](https://github.com/Detachm/macro-data-platform/issues/2)

## 背景

Issue #2 要求在实现 US adapter 前冻结两日 MVP 的美国数据源范围、端点、授权边界、公共字段映射、稳定 ID/checksum 规则和 `available_at` basis。工程规范要求引入关键数据源、修改去重主键、revision 或 PIT 策略时写 ADR。

本 ADR 记录的是两日 MVP 的文档级决策，不代表生产 live 接入或授权批准。

## 候选方案

1. 只写聚合 `us-mvp.md`，不拆 source-level 文件。
   - 拒绝：不满足 source-level owner、账号负责人、rights matrix 和退出方案要求。
2. 直接使用 FRED/ALFRED 统一取宏观和利率数据。
   - 拒绝：FRED 条款要求对第三方 series 遵守数据所有者的权利限制；逐 series owner permission 尚未复核。两日 MVP 因此采用平台 no-ingest/no-LLM 保守策略，而非把该平台策略表述为 FRED 的一概存储或 AI 禁令。
3. 把 GDELT 作为默认 news metadata live source。
   - 拒绝：underlying publisher rights 未完成复核，权限不明确按不允许处理。
4. 采用保守 MVP 矩阵：官方 numeric facts 为 live candidate，商业/不明确来源 fixture-only 或 disabled，source-level 文件逐一登记。
   - 采用：符合两日目标、PIT 约束和授权纪律。

## 决策

- US MVP 的冻结矩阵记录在 `docs/data-sources/us-mvp.md`。
- 每个候选来源单独维护 source-level 登记文件：Nasdaq Trader、SEC EDGAR、Polygon/Massive、Alpha Vantage、Federal Reserve、Treasury、BLS、BEA、FRED/ALFRED、GDELT、NewsAPI。
- 两日内无合同或权限不明确的商业行情、商业新闻、GDELT、FRED/ALFRED 均不得 live ingest；使用 synthetic/脱敏 fixture 完成 provider abstraction。
- 官方 numeric facts 的 live candidate 是 SEC metadata、Federal Reserve、Treasury、BLS、BEA；即使如此，两日 provider 仍先 fixture-backed，live smoke 需要显式凭据和 source approval。
- US MVP 的稳定 ID、`source.provider_record_id`、checksum、source URL 和排序键采用 `docs/data-sources/us-mvp.md` 中的公共 identity 表。US instrument ID 的唯一 seed 是 UTF-8 `canonical_symbol + first_valid_from`（ISO-8601 date，无分隔符）；`issuer_key`/SEC CIK 是可缺失 enrichment，绝不进入 seed。精确 golden 由 `tests/fixtures/us/normalization/instrument_id_cases.json` 共享。ID 不依赖抓取时间、分页位置、随机数或可选 enrichment。
- 无可信 provider dissemination timestamp 时，`available_at` 必须使用平台 `first_seen`。官方 release calendar 或 observation date 不能单独作为 PIT availability proof。
- SEC `acceptanceDateTime` 可作为 provider dissemination evidence 或 filing event time，但不能写成平台 `first_seen_at`。

## 后果

- 保持两日目标聚焦在 abstraction、contracts、fixtures、PIT 和 rights 边界，而不是采购或 live 接入。
- source-level 文件让每个来源的账号、授权、端点、失败、fixture 和退出方案可单独 review。
- 保守 rights 默认值降低新闻正文、行情数据和第三方内容误入 Git、日志或 LLM 的风险。
- #4/#6/#7 实现时必须引用本 ADR 和 `us-mvp.md` 的 ID/checksum/PIT 规则。
- 后续若更换 primary source、启用 FRED/GDELT/商业新闻、改变 ID 语义或放宽 rights，需要更新本 ADR 或新增 superseding ADR。
- 当前不创建 US 私有公共 DTO；所有 provider 输出仍走现有 `contracts/`。

上线方案：

1. 先合并 source matrix 和 ADR。
2. #4 实现 US normalization fixtures。
3. #6 实现 fixture-backed provider vertical slices。
4. #7 跑 shared provider contracts，并用 live smoke marker 控制所有真实请求。

回滚方案：

- 如 source matrix 被否决，revert 本 ADR 和对应 `docs/data-sources/us-*.md` 变更。
- 如果某来源授权被否决，保留 ADR，更新对应 source-level 文件为 disabled，并开替代来源 Issue。

## 验证

- 文档结构：每个 `docs/data-sources/us-*.md` source-level 文件必须包含 ownership、接口与覆盖、公共合同映射、权利矩阵、失败与降级、fixtures/测试、运行指标与退出方案。
- 测试 ID：后续 #4 覆盖 `SYM-004`～`SYM-010`、`TIME-005`、`TIME-006`、US unit tests；#6/#7 覆盖 applicable `PRV-001`～`PRV-021`、`NEWS-002`、`NEWS-003`、`NEWS-012`、`NEWS-013`、`NEWS-017` 和 PIT `available_at <= as_of`。
- 数据回放范围：两日 MVP 使用 synthetic/脱敏 fixture；无批准凭据不跑 live。
- 检查命令：`git diff --check`、`uv run ruff format --check .`、`uv run ruff check .`、`uv run mypy --strict src`、`uv run pytest -m "not live" --cov=macro_platform --cov-report=term-missing`。
- 验收人：@Detachm 最终批准；@Nouzee 对 US news/NewsEvent rights 做交叉评审。

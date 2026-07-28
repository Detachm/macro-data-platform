# ADR 0012：XtQuant 接入 HK 三大核心指数日线

- 状态：accepted
- 日期：2026-07-28
- 关联 Issue：[#50](https://github.com/Detachm/macro-data-platform/issues/50)、[#51](https://github.com/Detachm/macro-data-platform/issues/51)

## 背景

ADR 0008 只批准了十个 HK 个股，不能满足日报必需输入
`market.hk.core_indices.previous_close`。核心指数代码不能按市场惯例猜测，必须先通过项目现有付费
XtQuant data centre 的 instrument metadata 确认，再验证实际 `1d raw` 数据权限。

同一个 XtQuant provider 若用相同 provider role 分别调度指数和个股，两项请求的持久化幂等身份会
冲突，因为 `IngestJobRequest` 本身不携带 instrument allowlist。因此两类任务必须使用不同 role，
同时继续共享同一个 provider/client 和宿主机数据中心。

## 决策

- 必需任务 `hk.core-index-bars` 使用 `hk.bars.primary`，固定包含：
  - `HSI.HK` → `ins_hk_index_hsi` / `XHKG:HSI`；
  - `HSCEI.HK` → `ins_hk_index_hscei` / `XHKG:HSCEI`；
  - `HSTECH.HK` → `ins_hk_index_hstech` / `XHKG:HSTECH`。
- 可选任务 `hk.equity-bars` 使用 `hk.equity-bars.supplemental`，继续覆盖 ADR 0008 的十个个股。
  两个 role 必须解析到同一个 `HkXtQuantDailyBarsProvider` 实例，否则 worker 拒绝启动。
- `HK_XTQUANT_SYMBOLS` 的生产默认值包含三项指数和十个个股。配置缺少任一核心指数时，live
  composition fail closed；未审核 symbol 仍被 allowlist 拒绝。
- 指数发布日期采用恒生指数公司 2026-03-31 指数目录：HSI 为 1969-11-24，HSCEI 为
  1994-08-08，HSTECH 为 2020-07-27。它们只用于稳定 instrument contract，不冒充行情可见时刻。
- 行情仍以抓取时刻作为 `available_at`，`availability_basis=first_seen`；不声明历史 PIT 能力，
  不保存 SDK 原始响应或付费 payload。

## 权限探测证据

2026-07-28 在本机临时、仅回环监听的数据中心完成最小元数据探测：

- 8 个 HK index-like sector、744 个去重 symbol；1 个与目标无关的元数据项因当前权限不可读；
- `HSI.HK` 返回“恒生指数”、HK、产品类型 `-1`；
- `HSCEI.HK` 返回“国企指数”、HK、产品类型 `-1`；
- `HSTECH.HK` 返回“恒生科技指数”、HK、产品类型 `-1`；
- 三个目标均唯一，无缺失、无歧义；随后三项近期 `1d raw` live smoke 通过。

探测器只输出 identity metadata 与计数。无关 symbol 的权限异常可以跳过，但三项目标任一缺失或
出现多个精确匹配时仍返回失败。验证完成后临时 `127.0.0.1:58615` 服务已关闭。

## 后果与回滚

- HK 核心指数可成为冻结输入的 durable evidence；三个 instrument 的上一交易日数据必须齐全且
  通过 freshness/quarantine 检查，否则日报仍阻断并向预警群告警。
- 十个个股任务失败只造成 `degraded`，不会覆盖或伪装核心指数结果。
- 回滚时同时解除 `hk.core-index-bars` 与指数 allowlist，质量门禁会恢复为显式缺失；已持久化事实
  和审计记录保留。

## 参考

- 恒生指数公司指数目录：
  https://www.hsi.com.hk/static/uploads/contents/zh_cn/dl_centre/index_catalogue/index_catalogue.pdf
- 宿主机服务和探测手册：`docs/runbooks/xtquant-host-service.md`
- HK 日线运行手册：`docs/runbooks/xtquant-hk-bars.md`

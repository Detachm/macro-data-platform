# ADR 0008：通过 XtQuant 接入 HK 核心股票日线

- 状态：accepted
- 日期：2026-07-27
- 关联 Issue：[#28](https://github.com/Detachm/macro-data-platform/issues/28)

## 背景

#28 还缺少真实 HK 日线。项目已有 Beast 的 XtQuant 实现和由内部环境维护的数据中心；该进程
持有 vendor SDK、账户 token、缓存目录和监听端口。将启动过程复制到 macro worker 会造成多个
worker 争抢端口、重置 SDK 全局状态，甚至终止其他使用者。

## 决定

- 注册 `hk.xtquant.v1` 为 live `hk.bars.primary`，仅支持 `bars / HK / 1d / raw`。
- 初始静态 allowlist 为 10 个核心港股：`00700.HK`、`09988.HK`、`03690.HK`、`01810.HK`、
  `00941.HK`、`00005.HK`、`00388.HK`、`01299.HK`、`02318.HK` 和 `09618.HK`。部署可从此
  allowlist 选择子集，不能添加未审核标的。
- worker 只以 `HK_XTQUANT_HOST`/`HK_XTQUANT_PORT` 连接已运行 data-centre，调用
  `download_history_data2(..., "1d")` 和 `get_market_data_ex(..., fill_data=False)`；不接收
  token，不调用数据中心初始化、监听、关闭或端口清理。
- XtQuant SDK 是部署层 vendor 依赖，不作为 PyPI dependency 进入 lockfile。运行环境必须使用
  已批准 CPython 3.12 SDK 包，并通过 `PYTHONPATH` 或镜像安装提供 `xtquant` 模块。
- 返回的 `index`（`YYYYMMDD`）和 `time`（epoch milliseconds）必须对应同一香港交易日；日线
  记录以平台抓取时间作为 `available_at` 与 `first_seen`，因此拒绝历史 PIT 查询。
- 每页持久化 instrument、bar、原始日期时区审计、quarantine evidence 和 checkpoint watermark；
  不保存 token、SDK cache 或原始 data-frame response。

## 验证与回滚

- mock XtQuant client 覆盖连接参数、批量下载、字段映射、schema drift、cursor/snapshot 和
  checkpointed PostgreSQL 持久化；可选 live smoke 只查询 `00700.HK`。
- 回滚时解绑 `hk.bars.primary` 并停用对应 job；保留已入库规范化事实和审计记录，除非项目负责人
  要求删除。

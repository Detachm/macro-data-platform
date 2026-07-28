# XtQuant HK 日线运行手册

`HkXtQuantDailyBarsProvider` 只消费一个已运行的 XtQuant data-centre。按 Beast 的部署边界，
数据中心与 macro worker 分开运行：前者拥有 vendor SDK、`XTQUANT_TOKEN` 和本地缓存；后者不应
拥有 token，也绝不能清理或终止 data-centre 的监听端口。

宿主机 service、凭据边界、防火墙和指数权限探测的完整步骤见
[`xtquant-host-service.md`](xtquant-host-service.md)。

## 部署前提

- 为数据中心和 worker 使用与 Beast 验证过的 CPython 3.12 XtQuant vendor package；worker 可从
  `PYTHONPATH` 或镜像安装导入 `xtquant.xtdata`。
- 先启动 data-centre，并让它用 `XTQUANT_TOKEN`、自己的数据目录和唯一监听端口完成初始化。
- worker 设置 `HK_XTQUANT_HOST`、`HK_XTQUANT_PORT` 和 `HK_XTQUANT_SYMBOLS`。后者只能来自 ADR
  0008/0012 的 13 个审核代码；生产必须包含 `HSI.HK`、`HSCEI.HK`、`HSTECH.HK`。
- `PROVIDER_MODE=live` 时 factory 将必需指数 role `hk.bars.primary` 和可选个股 role
  `hk.equity-bars.supplemental` 绑定到同一个 XtQuant provider；macro 配置不设置或读取
  `XTQUANT_TOKEN`。
- 调度任务分别为必需的 `hk.core-index-bars` 和可选的 `hk.equity-bars`。前者缺失、过期或隔离时
  阻断正常日报；后者失败只将工作流标为 `degraded`。

## 验证

在已配置 vendor runtime 和共享 data-centre 的 worker 环境中执行：

```bash
RUN_LIVE_SMOKE=1 RUN_XTQUANT_LIVE_SMOKE=1 \
  uv run pytest tests/live/test_cn_hk_provider_smoke.py -k xtquant -q
```

该 smoke 包含十个个股中的 `00700.HK`，以及 `HSI.HK`、`HSCEI.HK`、`HSTECH.HK` 三项核心指数
最近两周的 `1d raw` 日线。连接失败时检查 data-centre 的健康、端口、SDK 版本和上游账户；不得把
token 或原始 SDK response 粘贴到日志、issue 或聊天中。

## 故障处理

- `XTQUANT_RUNTIME_MISSING`：worker 镜像没有批准的 vendor SDK；修复镜像或 `PYTHONPATH`，不在
  应用运行时下载 SDK。
- 认证或权限错误：由 data-centre owner 检查当前 token 的有效性和供应商权限；是否更换 token 由
  项目负责人决定，macro worker 不保存 token。
- 连接失败：确认 host/port 与 data-centre 实际监听地址一致，且没有多个服务争抢端口。
- schema 或日期冲突：保留 quarantine evidence，停止自动重试该页，按通用 provider failure
  runbook 复现并修复。

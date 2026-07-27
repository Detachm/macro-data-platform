# XtQuant HK 日线运行手册

`HkXtQuantDailyBarsProvider` 只消费一个已运行的 XtQuant data-centre。按 Beast 的部署边界，
数据中心与 macro worker 分开运行：前者拥有 vendor SDK、`XTQUANT_TOKEN` 和本地缓存；后者不应
拥有 token，也绝不能清理或终止 data-centre 的监听端口。

## 部署前提

- 为数据中心和 worker 使用与 Beast 验证过的 CPython 3.12 XtQuant vendor package；worker 可从
  `PYTHONPATH` 或镜像安装导入 `xtquant.xtdata`。
- 先启动 data-centre，并让它用 `XTQUANT_TOKEN`、自己的数据目录和唯一监听端口完成初始化。
- worker 设置 `HK_XTQUANT_HOST`、`HK_XTQUANT_PORT` 和 `HK_XTQUANT_SYMBOLS`。后者只能是 ADR
  0008 中十个代码的非空、逗号分隔子集。
- `PROVIDER_MODE=live` 时 factory 将 `hk.bars.primary` 绑定到 XtQuant；不需要在 macro 配置中
  设置或读取 `XTQUANT_TOKEN`。

## 验证

在已配置 vendor runtime 和共享 data-centre 的 worker 环境中执行：

```bash
RUN_LIVE_SMOKE=1 RUN_XTQUANT_LIVE_SMOKE=1 \
  uv run pytest tests/live/test_cn_hk_provider_smoke.py -k xtquant -q
```

该 smoke 仅读取 `00700.HK` 最近两周 `1d raw` 日线。连接失败时检查 data-centre 的健康、端口、
SDK 版本和上游账户；不得把 token 或原始 SDK response 粘贴到日志、issue 或聊天中。

## 故障处理

- `XTQUANT_RUNTIME_MISSING`：worker 镜像没有批准的 vendor SDK；修复镜像或 `PYTHONPATH`，不在
  应用运行时下载 SDK。
- 认证或权限错误：由 data-centre owner 检查并轮换 token；macro worker 不保存 token。
- 连接失败：确认 host/port 与 data-centre 实际监听地址一致，且没有多个服务争抢端口。
- schema 或日期冲突：保留 quarantine evidence，停止自动重试该页，按通用 provider failure
  runbook 复现并修复。

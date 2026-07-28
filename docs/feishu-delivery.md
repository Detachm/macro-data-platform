# 飞书日报交付（#32）

本模块只把已经通过事实校验、且 `publication.decision=published` 的不可变
`DailyReport` 投递为飞书 `interactive` 卡片。它不重新查询 provider、不重新生成
文本，也不检查外部 LLM 或来源权利 gate。

## 当前接入边界

- `ReportDeliveryService.deliver(..., dry_run=True)` 只生成卡片和幂等键，不写入
  `delivery_attempts`，也不向飞书发请求。
- 正式发送会以 `report_date + report_version + target chat` 生成稳定的幂等键；已成功的
  投递会直接复用已有审计记录，不会再次发送。发送请求还携带由该幂等键派生的 50 字符
  Feishu `uuid`，利用消息 API 的一小时服务端去重窗口抵御安全重试和短时重复请求。
- 审计记录保存报告版本、目标、尝试次数、状态、脱敏请求/响应、失败码和飞书
  `message_id`。迁移版本为 `0011`。
- 卡片固定展示报告日期、摘要、CN/HK/US 高亮、未来日程、数据质量和
  `source_references` 的可点击链接；所有文字均取自已校验的 `DailyReport`。
- 明确的限流可以在限定次数内重试。超时、断连和服务端异常可能已被飞书接收，状态会
  标记为 `uncertain`，默认不自动重发，须先人工核对群内消息再处理。
- 认证、卡片参数、群聊不可用和限流会分别记录为 `FEISHU_AUTH_FAILED`、
  `FEISHU_CARD_INVALID`、`FEISHU_CHAT_UNAVAILABLE`、`FEISHU_RATE_LIMITED`；保留
  脱敏后的飞书原始响应以便排查。
- `PostgresReportDeliveryStore` 会在任何网络发送前独立提交 `pending` 预留，并独立提交每次
  终态变更；进程在飞书已接收但本地未更新时重启，只会看到既有 `pending`，不会自动重发。

完整自动交付编排尚由 #33 接入。因此在该任务完成前，保持
`FEISHU_DELIVERY_ENABLED=false`；服务本身不会在启动时发送任何消息。#33 应创建并持有
HTTP client，并构造每次状态变更独立提交的 `PostgresReportDeliveryStore`，再通过
`ConfiguredFeishuDelivery(settings=..., client=..., store=...).deliver(...)` 执行投递，
使目标群、超时、重试次数和持久化边界始终来自运行时组合根。

## 联调前由部署方完成

1. 在飞书开放平台创建或选用一个自建应用，启用机器人能力与“以应用身份发送消息”权限。
2. 发布该应用到内部租户，并把机器人加入一个专门的测试群。
3. 在部署的密钥管理环境配置以下变量；不要把 App Secret 发到 issue、PR、聊天记录或
   `.env.example`：

   ```dotenv
   FEISHU_DELIVERY_ENABLED=true
   FEISHU_APP_ID=cli_xxx
   FEISHU_APP_SECRET=...
   FEISHU_CHAT_ID=oc_xxx
   FEISHU_API_BASE_URL=https://open.feishu.cn
   FEISHU_TIMEOUT_SECONDS=15
   FEISHU_DELIVERY_MAX_ATTEMPTS=3
   ```

4. 先对一份已验证的报告执行 dry-run，确认卡片预览与目标群；再开启一次真实测试发送。

飞书自建应用获取 `tenant_access_token` 并通过消息接口发送卡片的前提与接口说明见
[飞书消息 API 概览](https://open.feishu.cn/document/server-docs/im-v1/introduction?lang=zh-CN)
及 [tenant_access_token 文档](https://open.feishu.cn/document/ukTMukTMukTM/uMTNz4yM1MjLzUzM)。

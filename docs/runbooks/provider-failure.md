# Runbook：Provider 抓取失败或数据过期

## 触发条件

- provider health 为 `down` 或连续失败超过策略阈值；
- watermark 长时间不推进；
- freshness、完整率或 schema drift 告警；
- 401/403、429、HTML 登录页、超时或未知字段导致批次隔离。

## 处置

1. 根据 `request_id`、`run_id`、provider role 和 dataset 定位失败批次，禁止在工单或日志中粘贴密钥/正文。
2. 核对上游状态、凭据、配额和数据源登记文档；401/403 不做无限重试。
3. 检查最近成功 watermark、rejection 样本和 schema checksum，不手工跳过 checkpoint。
4. 若允许 fallback，显式切换 provider role，并让 coverage/provenance 标记 degraded；禁止无痕混源。
5. 修复 adapter 时先加入能复现的脱敏 fixture，再运行公共 contract suite。
6. 从最后已提交 watermark 回放；比较业务主键、checksum 和行数，确认重放无重复。
7. 恢复后补齐缺口，确认 freshness 和质量门槛，再关闭事件。

## 升级

- 凭据疑似泄漏或账号权限异常：立即停止任务并轮换凭据。
- 公共 contract/API/migration 需要变化：提交 ADR，不在 provider 热修中夹带破坏性修改。
- 三地区日报关键数据均不可用：使用 `fail_on_incomplete=true` 阻断生成，不让 LLM 猜测缺失事实。

## 事件记录

记录影响窗口、受影响数据集、根因、是否有未来数据泄漏/正文泄漏、回放范围、校验结果和后续行动 owner。

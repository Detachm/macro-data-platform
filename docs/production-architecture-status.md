# 宏观日报生产架构与落地状态

## 1. 目标与已确认决策

本系统每天生成一份可追溯、可验证且只投递一次的 CN/HK/US 宏观日报。当前生产规则为：

- 时区统一使用 `Asia/Shanghai`。
- `07:50` 开始采集，`08:15` 冻结日报输入，`08:30` 发布。
- 输入完整时向日报群发送已验证报告。
- 输入不完整或流程终止失败时，不发送正常日报，改向预警群发送原因和安全处置指引。
- 周末及节假日仍发送宏观消息、事件日历和分析；休市市场栏目明确显示休市，不把正常休市误判为数据缺失。
- HK 行情优先复用 Beast 的付费 XtQuant 数据能力。
- 首次部署使用本机单节点 K8s；PostgreSQL 在线数据位于 NVMe PVC，备份写入独立的 `/archive` 物理盘。

## 2. 端到端数据流

```text
Live providers / Beast XtQuant
          |
          v
07:50 scheduled ingestion
          |
          v
PostgreSQL normalized facts + raw/audit evidence + checkpoints
          |
          v
08:15 immutable ReportInputSnapshot + data-quality gate
          |
          +---- blocked/retryable ------------------------+
          |                                               |
          v                                               v
Report generation (LLM or deterministic fallback)   Feishu alert chat
          |
          v
Factual, citation, date and publication validation
          |
          +---- invalid/incomplete -----------------------+
          |
          v
08:30 idempotent Feishu report delivery
          |
          v
Delivery audit + message_id + metrics + backup
```

外部 API 不在用户查询请求中临时调用。所有报告事实必须先持久化，再进入固定输入快照。

## 3. 运行组件

| 组件 | 职责 | 计划运行位置 |
| --- | --- | --- |
| XtQuant data centre | 使用付费权限提供 HK 行情、指数及可用扩展数据 | K8s 外的宿主机受限服务，端口 `58615` |
| `macro-data-worker` | 定时采集、质量门禁、报告生成、校验和投递编排 | K8s Deployment，单副本起步 |
| API | 健康检查、事实查询及后续受保护的运维入口 | K8s Deployment |
| PostgreSQL | 事实、快照、报告版本、运行审计和投递幂等 | K8s StatefulSet + NVMe PVC |
| migration Job | 在应用升级前执行 Alembic 单头迁移 | K8s Job |
| backup CronJob | 生成逻辑备份并验证备份可读性 | K8s CronJob，写入 `/archive` |
| Feishu application bot | 正常日报和异常预警 | 两个独立内部群 |

单节点部署不提供节点级高可用；当前目标是保证 Pod 重启可恢复、数据库有独立磁盘备份、失败不会重复投递。

## 4. 数据源与质量边界

### 已接入

- CN：BaoStock 核心指数日线、NBS 发布日历候选任务。
- HK：XtQuant 已审核个股日线、HKMA 官方标题。
- US：Twelve Data 的 SPY/QQQ/DIA 日线。
- fixture 仅用于离线测试，生产环境禁止回退到 fixture。

### 尚需补齐

- XtQuant HK 核心指数，例如恒生指数、恒生科技指数；实际 source symbol 必须通过付费数据中心能力探测确认，不能猜测。
- CN 官方新闻。
- HK/US 宏观发布日历。
- 宿主机 XtQuant `58615` 常驻服务及健康检查。

质量门禁对必需输入执行缺失、过期、迟到、隔离、revision 和可用性检查。缺口必须显式记录，禁止用历史行、合成数据或 fixture 冒充 live 数据。

## 5. 报告与投递安全

- 每次报告只消费一个不可变 `ReportInputSnapshot`。
- LLM 输出必须通过数字、日期、方向、单位、来源和引用校验。
- LLM 不可用但事实充分时，生成确定性 fallback。
- 事实不足时生成 `not_published` 结果，并向预警群告警。
- 飞书幂等范围为 `report_date + report_version + target chat`。
- 数据库永久保存幂等记录；飞书请求同时使用稳定 `uuid` 抵御短时重复请求。
- 超时或断连造成的模糊结果标记为 `uncertain`，禁止自动重发，必须先核对群内消息。
- App Secret、数据库密码和服务 token 只来自运行时 Secret，不进入 Git、日志或投递审计。

## 6. K8s 与存储设计

计划采用本机单节点 K3s。当前本机尚未安装 `kubectl`、K3s、Helm 或其他 K8s 发行版。

PostgreSQL 设计：

- `PGDATA` 位于 NVMe-backed PVC，不使用 `emptyDir`。
- 初始容量建议 50–100 GiB，容量告警阈值为 70%/85%。
- 每日投递完成后于 `09:00` 备份到 `/archive/macro-data-platform/postgres-backups`。
- 数据库迁移前额外生成一次备份。
- 建议保留 14 个日备份和 8 个周备份，并每月执行恢复演练。
- `/archive` 为独立 XFS 物理盘，但根目录为共享临时目录权限；备份必须写入权限为 `0700` 的专用子目录，文件权限为 `0600`。

该备份可以抵御 NVMe/PVC 损坏，但不能抵御整机损毁；异机或对象存储备份属于后续增强，不阻塞首次内部上线。

## 7. Secret 与网络边界

- XtQuant token 只属于宿主机 Beast/XtQuant 服务。K8s 工作负载只获得受限的 host/port，不获得该 token。
- Beast 中已经进入 Git 历史的明文 XtQuant token 必须轮换，并迁入宿主机受限 secret/EnvironmentFile。
- `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、日报群 Chat ID、预警群 Chat ID 和数据库密码进入 K8s Secret。
- K8s Pod 到宿主机 `58615` 的连接只允许宏观 worker，禁止公开暴露该端口。
- 正式飞书凭据不得经聊天、Issue、PR、截图或 shell history 传递。

## 8. 当前完成状态

### 已完成

- 公共 contracts、三地区标准化、PIT、持久化及审计底座。
- 已审核 live provider 的基础适配和离线合同测试。
- 不可变输入快照、报告版本、生成、事实校验和 fallback。
- `07:50` 采集、`08:15` 截止、质量门禁、重试、锁和 backfill。
- 飞书 JSON 2.0 卡片、dry-run、错误分类、永久幂等和投递审计。
- PR #46 已合并并关闭 #29。
- 已通过飞书官方一键创建流程创建新的自建应用；正式 App Secret 只写入本机权限为
  `0600` 且被 Git 忽略的 `.env`，未写入聊天、Issue、PR 或命令输出。
- 已创建两个独立私有群，授权用户已加入；日报群卡片和预警群文本测试消息均发送成功。
- 项目自身的 `ConfiguredFeishuDelivery`、PostgreSQL 审计和永久幂等已完成真实飞书验收：
  dry-run 不落库，首次发送成功并保存 `message_id`，重复调用没有再次发送，数据库仅有一条
  delivery attempt。
- PR #47 已合并并关闭 #32。
- PR #52 已合并：新增独立预警群、红色终态告警、迁移 `0012`、永久幂等和模糊结果禁止重发；
  真实预警群验收确认重复调用只产生一条消息和一条审计记录。
- `macro-data-worker` 已串起 ingest → quality → generate/fallback → validate → 08:30 deliver；
  workflow run、snapshot、report、delivery 和 alert ID 已进入结构化日志。
- 已增加 PostgreSQL + mocked provider/LLM/Feishu 的完整链路 E2E；同一报告日期复放时只调用一次
  LLM、只生成一份报告且只向日报群发送一次。
- 单日手工恢复支持显式不可变 `--report-version`；同一日期/版本重放保持幂等。
- 已增加受 Bearer token 保护的 worker readiness、按日脱敏状态查询和人工投递恢复 API；人工恢复以
  `X-Request-ID` 和迁移 `0013` 的 `delivery_operator_actions` 永久审计，`uncertain` 必须人工确认
  群内无消息，API 不接收 Chat ID。
- #50 仓库侧已增加 fail-closed 的宿主机 XtQuant data-centre CLI、systemd unit、TCP 自检和只输出
  HSI/HSTECH 标识元数据的 entitlement probe；真实 token 轮换、Beast 139 个 tracked 配置清理、
  防火墙、服务启动和指数 allowlist 仍需现场验收。

### 尚未完成

1. 按 #50 轮换 Beast 明文 XtQuant token，恢复并托管 `58615` 数据中心，同时扩展 HK
   核心指数。
2. 按 #51 补齐 CN 新闻、HK/US 日历和节假日报告策略。
3. 实现周末/节假日宏观版，不把休市当作必需行情缺失。
4. #49 的三阶段 K3s 清单和详细运行手册已经进入仓库：包括 Namespace、Deployment、
   StatefulSet、Service、migration Job、CronJob、100 Gi PVC、readiness、NetworkPolicy、运行时
   Secret 创建流程、`/archive` 备份、70%/85% 幂等预警和隔离恢复演练；尚未在本机安装 K3s 或执行
   真实 PVC/重启/恢复验收。
5. 在维护窗口安装固定版本 K3s，执行 #49 的宿主机验收，并确认最终值班 ownership。
6. 完成 provider 连续两个报告日验证和完整链路连续五个工作日 soak。

GitHub 当前开放的生产任务是 #33、#49、#50 和 #51，分别跟踪编排、部署/存储、
Beast/HK 行情和新闻/日历/节假日策略；不重新打开已经完成的旧 provider issue。

## 9. 落地顺序与上线门槛

推荐顺序：

1. 完成 #50：轮换已暴露的 XtQuant token，恢复宿主机 `58615` 常驻服务并接入付费
   XtQuant HK 核心指数。
2. 完成 #51：补齐 CN 新闻、HK/US 日历及节假日报告策略。
3. 完成 #49：安装 K3s，完成 PostgreSQL PVC、Secret、迁移和 `/archive` 备份。
4. 收口 #33 的 K8s 运维所有权和五工作日 soak；周末策略由 #51 提供输入。
5. 执行 E2E、两日报告日 provider 验证和五工作日生产式 soak。

只有同时满足以下条件才称为生产可用：

- 08:30 前生成明确的正常日报或阻断告警。
- 同一报告日期不会并发处理，同一报告版本不会重复发送。
- 所有事实均可追溯到固定输入快照和来源记录。
- 所有终态失败均通知预警群并给出安全恢复方法。
- Pod/worker 重启后可从 PostgreSQL 状态恢复。
- PostgreSQL 备份可实际恢复，而不只是成功生成文件。
- 连续五个工作日无重复投递且按时完成。

## 10. 当前外部连通性证据

2026-07-28 完成以下外部连通性与交付验收：

- 使用已经废弃的旧应用做过一次只读鉴权探测；旧凭据不再使用。
- 通过飞书官方一键创建流程重新创建应用，新的 App Secret 直接落入本机受限 `.env`，全过程只
  输出布尔状态，不输出 Secret 或 token。
- 新应用 `tenant_access_token` 鉴权返回 HTTP `200`、业务码 `0`，token 有效期 `7200` 秒。
- 新建日报群与预警群并加入授权用户；两个群的测试消息均返回业务码 `0` 和 `message_id`。
- 在一次性 PostgreSQL 上从空库迁移至 `0012`，使用项目正式投递类执行 dry-run、真实 interactive
  card 发送和同报告重复调用。首次投递为 `succeeded`，重复调用复用同一成功记录，数据库记录数
  严格为 `1`。
- 使用项目正式预警类向预警群发送终态测试卡片并复放，群消息和 PostgreSQL alert attempt 均严格
  为 `1`。

Chat ID、App ID、App Secret、token 和 `message_id` 不写入本文。当前本机
`FEISHU_DELIVERY_ENABLED=false`，防止开发环境自动误发；生产开关最终由 K8s Secret
和部署配置控制。

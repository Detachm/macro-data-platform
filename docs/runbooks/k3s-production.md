# 单节点 K3s 生产部署与 PostgreSQL 恢复手册

本手册对应 GitHub #49。目标是在本机单节点 K3s 上运行 API、worker 和 PostgreSQL，并把每日可验证
逻辑备份写入独立的 32 TB `/archive` 物理盘。

> 当前安全边界：仓库清单已经可审查；执行第 3 节会修改宿主机系统，必须由项目负责人明确批准并
> 安排维护窗口。不要在现有宿主机 PostgreSQL 上执行本手册，也不要改动宿主机已有的 `5432` 服务。

## 1. 组件与发布阶段

部署严格分三阶段：

1. `phase-1-infrastructure`：Namespace、配置、NetworkPolicy、PostgreSQL StatefulSet/PVC 和
   09:00 备份 CronJob。
2. `phase-2-migration`：单次 Alembic Job；失败时停止发布。
3. `phase-3-workloads`：API、单副本 worker 和 `/archive` 容量监控。

清单不包含任何 Kubernetes `Secret`。真实值只从仓库外的权限为 `0600` 的临时 env 文件创建。
XtQuant token 永远留在宿主机 Beast 服务；Pod 只取得节点 IP 和 `58615` 端口。

## 2. 已确认的宿主机存储

- `/archive`：独立 `/dev/sda1`、XFS、约 29.1 TiB，可用于数据库备份。
- K3s 默认 `local-path` 位于 `/var/lib/rancher/k3s/storage`；上线前必须用 `findmnt` 确认它落在
  NVMe 根盘，而不是 `/archive`。
- 宿主机已有其他 PostgreSQL 占用 `5432`；本项目的 Service 仅为 ClusterIP，不使用 host port，
  两者不冲突。
- 单节点不提供节点级高可用。PVC 解决 Pod 重建，`/archive` 备份解决在线 NVMe/PVC 损坏；整机损毁
  仍需后续异机或对象存储副本。

## 3. 安装 K3s（维护窗口内执行）

截至 2026-07-28，K3s stable channel 指向 `v1.36.2+k3s1`。生产安装固定版本，禁用未使用的
Traefik/ServiceLB，并启用 Secret 静态加密。升级前重新核对
[K3s release channels](https://docs.k3s.io/upgrades/manual) 和对应
[release notes](https://github.com/k3s-io/k3s/releases/tag/v1.36.2%2Bk3s1)。

```bash
findmnt -T /var/lib
curl -sfL https://get.k3s.io \
  | INSTALL_K3S_VERSION='v1.36.2+k3s1' \
    INSTALL_K3S_EXEC='server --disable=traefik --disable=servicelb --secrets-encryption' \
    sh -

sudo systemctl is-active k3s
sudo k3s kubectl get nodes -o wide
sudo k3s kubectl get storageclass local-path
findmnt -T /var/lib/rancher/k3s/storage
```

验收要求：节点为 `Ready`，`local-path` 存储目录位于 NVMe 文件系统，且 K3s 重启后仍能恢复。
K3s stable channel 推荐用于生产；CronJob `.spec.timeZone` 在 Kubernetes v1.27 已稳定，本版本可直接
使用 `Asia/Shanghai`。

## 4. 准备 `/archive` 专用目录

备份 Pod 与容量监控以 UID/GID `999` 访问专用目录。只创建这一条明确路径，不修改 `/archive`
根目录现有权限。

```bash
findmnt -T /archive
sudo install -d -o 999 -g 999 -m 0700 \
  /archive/macro-data-platform/postgres-backups
sudo stat -c '%U %G %a %n' /archive/macro-data-platform/postgres-backups
```

预期权限为 `999:999 700`。备份脚本会把 `daily/`、`weekly/` 保持为 `0700`，dump 文件保持为
`0600`。

## 5. 构建并导入应用镜像

当前清单固定本机镜像 `macro-data-platform:0.1.0`，并设置 `imagePullPolicy: Never`，避免单节点
误拉取同名外部镜像。发布新版本时先更新仓库版本/清单标签，走 PR 和 CI，再导入对应镜像。

```bash
docker build --tag macro-data-platform:0.1.0 .
image_tar="$(mktemp --suffix=.macro-data-platform.tar)"
docker save --output "$image_tar" macro-data-platform:0.1.0
sudo k3s ctr images import "$image_tar"
rm -f "$image_tar"
sudo k3s ctr images list | rg 'macro-data-platform.*0.1.0'
```

镜像必须包含 `/app/alembic.ini` 和 `/app/migrations`；否则迁移 Job 会失败并阻断部署。

## 6. 创建运行时 Secret

先在仓库外创建临时目录。不要把真实值写进命令参数、shell history、Issue、PR 或聊天。

```bash
secret_dir="$(mktemp -d)"
chmod 0700 "$secret_dir"
install -m 0600 \
  deploy/k3s/production/phase-1-infrastructure/postgres.env.example \
  "$secret_dir/postgres.env"
install -m 0600 \
  deploy/k3s/production/phase-1-infrastructure/runtime.env.example \
  "$secret_dir/runtime.env"
```

用本机编辑器填写两份文件：

- `POSTGRES_PASSWORD` 使用新的高熵、URL-safe 密码。
- `DATABASE_URL` 使用
  `postgresql+asyncpg://macro:<same-password>@postgres.macro-data-platform.svc.cluster.local:5432/macro_data`。
- `runtime.env` 填入 service token、provider cursor secret、Twelve Data 凭据、新飞书应用凭据以及
  两个不同的 Chat ID。
- 不要填写 XtQuant token；它不属于 Kubernetes Secret。

确认没有空值后再创建 Secret。以下命令只传文件路径，不会把明文放入命令行：

```bash
if rg '=$' "$secret_dir/postgres.env" "$secret_dir/runtime.env"; then
  echo '存在空 Secret，停止部署'
  false
fi

sudo k3s kubectl create namespace macro-data-platform --dry-run=client -o yaml \
  | sudo k3s kubectl apply -f -
sudo k3s kubectl -n macro-data-platform create secret generic macro-postgres \
  --from-env-file="$secret_dir/postgres.env" --dry-run=client -o yaml \
  | sudo k3s kubectl apply -f -
sudo k3s kubectl -n macro-data-platform create secret generic macro-runtime \
  --from-env-file="$secret_dir/runtime.env" --dry-run=client -o yaml \
  | sudo k3s kubectl apply -f -

rm -f "$secret_dir/postgres.env" "$secret_dir/runtime.env"
rmdir "$secret_dir"
```

只检查键名和资源存在性，禁止执行 `kubectl get secret -o yaml` 或解码 Secret。

## 7. 分阶段部署

### 7.1 基础设施

```bash
sudo k3s kubectl apply -k deploy/k3s/production/phase-1-infrastructure
sudo k3s kubectl -n macro-data-platform rollout status statefulset/postgres --timeout=5m
sudo k3s kubectl -n macro-data-platform get pvc,pod,cronjob
```

PVC 必须为 `Bound`、容量 `100Gi`、StorageClass 为 `local-path`。PostgreSQL Pod 必须为 `Ready`。

### 7.2 迁移发布门

首次部署可以直接迁移；后续部署必须先执行一次迁移前备份：

```bash
backup_job="postgres-backup-predeploy-$(date +%Y%m%d%H%M%S)"
sudo k3s kubectl -n macro-data-platform create job \
  --from=cronjob/postgres-backup "$backup_job"
sudo k3s kubectl -n macro-data-platform wait \
  --for=condition=complete "job/$backup_job" --timeout=60m
```

随后重建不可变的 migration Job：

```bash
sudo k3s kubectl -n macro-data-platform delete job macro-data-migration \
  --ignore-not-found
sudo k3s kubectl apply -k deploy/k3s/production/phase-2-migration
sudo k3s kubectl -n macro-data-platform wait \
  --for=condition=complete job/macro-data-migration --timeout=15m
sudo k3s kubectl -n macro-data-platform logs job/macro-data-migration
```

如果 Job 失败，停止，不应用 `phase-3-workloads`。先保存日志、定位迁移问题，再决定修复向前或从已验证
备份恢复；禁止盲目执行 Alembic downgrade。

### 7.3 API、worker 和容量监控

只有 #50 的宿主机 XtQuant 服务已经安全监听节点可达地址、#51 的生产输入已就绪时才启动 worker：

XtQuant 安装与防火墙验收按
[`xtquant-host-service.md`](xtquant-host-service.md) 执行；K8s Secret 中不得出现 XtQuant token。

```bash
sudo k3s kubectl apply -k deploy/k3s/production/phase-3-workloads
sudo k3s kubectl -n macro-data-platform rollout status deployment/macro-data-api --timeout=5m
sudo k3s kubectl -n macro-data-platform rollout status deployment/macro-data-worker --timeout=5m
sudo k3s kubectl -n macro-data-platform get pods,cronjobs
```

API 仅暴露 ClusterIP。运维访问使用临时 port-forward，不创建公网 Ingress：

```bash
sudo k3s kubectl -n macro-data-platform port-forward service/macro-data-api 8000:8000
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/ready
```

受保护的 worker readiness 还需使用 service token；不要把 token 写进共享终端记录。

## 8. 首次上线验收

### 8.1 PVC 重建与重启

记录当前迁移版本，删除 PostgreSQL Pod，确认 StatefulSet 使用同一 PVC 重建：

```bash
sudo k3s kubectl -n macro-data-platform exec statefulset/postgres -- \
  sh -ec 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc \
  "select version_num from alembic_version"'
sudo k3s kubectl -n macro-data-platform delete pod postgres-0
sudo k3s kubectl -n macro-data-platform rollout status statefulset/postgres --timeout=5m
sudo k3s kubectl -n macro-data-platform get pvc
```

维护窗口内再执行一次宿主机重启验收；K3s、PVC、PostgreSQL、API 和 worker 均应自动恢复。不要在
08:15–08:30 发布窗口做重启演练。

### 8.2 手工备份

```bash
backup_job="postgres-backup-smoke-$(date +%Y%m%d%H%M%S)"
sudo k3s kubectl -n macro-data-platform create job \
  --from=cronjob/postgres-backup "$backup_job"
sudo k3s kubectl -n macro-data-platform wait \
  --for=condition=complete "job/$backup_job" --timeout=60m
sudo k3s kubectl -n macro-data-platform logs "job/$backup_job"
sudo find /archive/macro-data-platform/postgres-backups \
  -maxdepth 2 -type f -name '*.dump' -printf '%m %u:%g %s %p\n'
```

验收要求：日志为 `postgres_backup status=succeeded`；不存在 `.partial`；dump 为 `0600`；日备份最多
14 个、周备份最多 8 个。

### 8.3 从最新 dump 恢复到全新临时 PostgreSQL

restore drill 只使用 `emptyDir`，不挂载在线 PVC；完成后删除 Pod 即可销毁临时副本。

```bash
sudo k3s kubectl -n macro-data-platform delete pod postgres-restore-drill \
  --ignore-not-found
sudo k3s kubectl apply -k deploy/k3s/production/restore-drill
sudo k3s kubectl -n macro-data-platform wait \
  --for=condition=ready pod/postgres-restore-drill --timeout=5m

sudo k3s kubectl -n macro-data-platform exec postgres-restore-drill -- sh -ec '
  latest="$(find /archive/backups/daily -maxdepth 1 -type f -name "*.dump" | sort | tail -n 1)"
  test -n "$latest"
  PGPASSWORD="$POSTGRES_PASSWORD" pg_restore \
    --exit-on-error --clean --if-exists --no-owner --no-privileges \
    --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" "$latest"
  PGPASSWORD="$POSTGRES_PASSWORD" psql \
    --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --tuples-only --no-align \
    --command="select version_num from alembic_version"
'

sudo k3s kubectl -n macro-data-platform delete pod postgres-restore-drill
```

每月执行一次，并把日期、备份文件名、恢复耗时和迁移版本记入运维审计。仅生成 dump、不做恢复，不算
备份验收完成。

## 9. 容量、告警和日常检查

- `postgres-backup`：每天 `09:00 Asia/Shanghai`，原子生成 custom-format dump，先执行
  `pg_restore --list`，再改名为正式文件。
- `archive-capacity-monitor`：每 15 分钟只读检查 `/archive`；达到 70% 向预警群发送 warning；达到
  85%、专用目录不可访问，或文件系统总容量低于 20 TB（典型的掉挂载/误落系统盘）时发送 critical
  并让 CronJob 失败。
- 同一日期、同一级别使用稳定 workflow ID 和 PostgreSQL alert audit 幂等，不会每 15 分钟重复刷群。
- 每日 09:10 检查最近 backup Job；每月恢复；每季度检查异机备份方案。

常用只读命令：

```bash
sudo k3s kubectl -n macro-data-platform get pods,pvc,jobs,cronjobs
sudo k3s kubectl -n macro-data-platform get events --sort-by=.lastTimestamp
sudo k3s kubectl -n macro-data-platform logs deployment/macro-data-worker --tail=200
sudo k3s kubectl -n macro-data-platform logs deployment/macro-data-api --tail=200
```

日志中禁止出现数据库密码、API key、App Secret、Chat ID、provider 原始付费 payload 或飞书 token。

## 10. 回滚边界

- **应用失败、迁移成功且向后兼容**：保留 schema，导入上一版镜像，恢复上一版明确标签并重新应用
  phase 3。worker 为 `Recreate`，数据库 advisory lock 和投递幂等仍是最后防线。
- **迁移失败**：phase 3 不发布；保留失败日志和现有 PVC。优先修复并向前迁移。
- **破坏性 schema 问题**：停止 API/worker，保留在线 PVC，先把迁移前 dump 恢复到临时 PostgreSQL
  验证；项目负责人确认 RPO 后才能替换在线库。
- **PVC/在线 NVMe 损坏**：新建 PVC/PostgreSQL，从最近验证 dump 恢复，再启动应用。不要删除旧 PVC，
  先标记和隔离。
- **K3s 升级失败**：按官方 K3s rollback 文档恢复 K3s 数据目录快照和固定旧版本；应用数据库 dump
  不能替代 K3s 自身状态备份。

任何回滚前先确认飞书日报群是否已有消息；`uncertain` 投递不能自动重发。

## 11. Ownership 与完成定义

| 项目 | 主责 | 复核 |
| --- | --- | --- |
| K3s 安装、升级、节点重启 | 平台负责人 | 项目负责人 |
| PostgreSQL 迁移、PVC、备份与恢复 | 后端负责人 | 平台负责人 |
| XtQuant 宿主机服务、token 与防火墙 | Beast/HK 数据负责人 | 平台负责人 |
| 飞书应用、两群权限与 Secret 轮换 | 项目负责人 | 后端负责人 |
| 08:30 日报值守与人工恢复 | 当日值班人 | 项目负责人 |

#49 只有在以下证据齐全后才能关闭：Pod 删除与主机重启数据仍在；迁移失败确实阻断发布；真实 dump
恢复到全新临时 PostgreSQL；70%/85% 告警可见且幂等；Pod 不含 XtQuant token；清单、日志和 CI 无
Secret；连续运行观察没有备份失败。

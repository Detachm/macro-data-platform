# XtQuant 宿主机服务、token 轮换与指数权限探测

本手册对应 #50。目标是把付费 XtQuant data centre（数据中心）作为独立、受监督的宿主机服务运行，
只允许 K3s 中的宏观 worker 访问，并用最小化元数据确认恒生指数与恒生科技指数的真实 source
symbol。禁止猜代码，也禁止把 token 或付费行情 payload 写入 Git、日志、Issue 或聊天。

## 1. 当前事实与安全边界

2026-07-28 只读核查结果：

- 宿主机 `58615` 当前没有监听。
- Beast 活跃工作树存在大量其他人的未提交改动，不能直接做批量 token 清理。
- Beast 当前分支有 159 个 tracked YAML，其中 139 个 `xtquant_server.token` 为非空；旧 token 已进入
  共享 Git 历史，必须在供应商侧撤销。本文不显示、复制或散列该 token。
- Beast 旧启动函数会主动杀掉占用目标端口的进程；生产服务不得复用这种行为。本项目的新入口遇到端口
  占用会直接失败，让值班人判断冲突来源。
- 已验证的 XtQuant CPython 3.12 vendor package 可以从 Beast 的 versioned `site-packages` 目录导入；
  宿主机服务通过受控 `PYTHONPATH` 使用它，K8s 镜像不携带 token。
- 当前账号有 sudo 但需要交互密码，因此本轮没有读防火墙规则、创建系统用户、安装 unit 或启动服务。

仓库提供：

- `macro-data-xtquant-server serve`：从环境读取 token，初始化 HK 市场并在明确 IP/端口监听。
- `macro-data-xtquant-server check`：只做 TCP readiness（就绪检查），不读取 token、不取行情。
- `macro-data-xtquant-probe`：只扫描 sector/instrument identity metadata（板块/合约标识元数据），输出
  HSI/HSTECH 的匹配标识，不输出价格、成交量、原始响应或凭据。
- `deploy/systemd/xtquant-data-center.service`：自动重启、启动后等待 readiness、最小权限和文件系统保护。

## 2. 必须由项目负责人完成：供应商 token 轮换

这是唯一不能由仓库代码代替的步骤。

1. 在 XtQuant/迅投的账号管理入口创建新的独立 production token。
2. 不要在聊天、命令参数、工单或普通文本编辑器历史中传递。
3. 先把新 token 写入第 5 节的 root-only EnvironmentFile（环境文件）并完成一次服务鉴权。
4. 随即在供应商侧撤销旧 token；不能只从当前文件删除。
5. 记录轮换时间、操作人和供应商返回的撤销成功状态，但不记录 token。

旧 token 已在 Git 历史中，因此即使清理当前分支也仍按泄露处理。除非 Beast 仓库安全负责人决定统一重写
共享历史，否则不要擅自 force-push；撤销旧 token 才是首要控制。

## 3. Beast 当前分支清理要求

由 Beast/HK 数据负责人从干净分支单独提交，不要在当前脏工作树上批量改：

- 139 个 tracked YAML 的 `xtquant_server.token` 改为空或删除。
- loader 从 `XTQUANT_TOKEN` 环境读取；生产时 YAML 非空 token 应直接报错，防止旧模式回流。
- fixture 使用明确的 `test-only` 值，不复用真实格式或真实长度。
- CI 扫描当前 tree 和新增 commit，证明无 token；不得在 CI 打印命中行。
- 活跃 checkout 清理完成后，再用“仅计数、不显示值”的脚本复核非空 tracked token 数为 0。

本项目不在 Beast 脏工作树中自动执行这些修改，以免覆盖现有 Mammoth 任务和研究数据。

## 4. K3s 与防火墙前置

先完成 #49 的 K3s 安装，并取得节点 InternalIP 与 Pod CIDR。服务绑定 InternalIP，CLI 会拒绝
`0.0.0.0`；仅绑定 IP 仍不等于网络隔离，启用服务前必须加宿主机防火墙。

```bash
node_ip="$(sudo k3s kubectl get node -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
pod_cidr="$(sudo k3s kubectl get node -o jsonpath='{.items[0].spec.podCIDR}')"
test -n "$node_ip"
test -n "$pod_cidr"
printf 'node_ip configured=%s, pod_cidr configured=%s\n' "yes" "yes"
```

平台负责人根据实际 CNI interface（容器网络接口）配置规则：只允许 `pod_cidr` 经 K3s CNI 到
`node_ip:58615/tcp`，随后拒绝 LAN、Tailscale、Docker bridge 和其他来源。UFW 示例必须在现场先核对
现有顺序，不能盲目追加：

```bash
sudo ufw status numbered
ip route show "$pod_cidr"
# 复核接口后，由平台负责人插入“Pod CIDR allow”与后续“其他来源 deny”。
```

验收要从三处做：worker Pod 可连接；宿主机明确地址可自检；另一台 LAN 主机连接必须失败。当前 #49
NetworkPolicy 只给 worker 开放 `58615` egress，API/备份/监控均没有该端口权限，但 NetworkPolicy
不能替代宿主机防火墙。

## 5. 安装 systemd（系统服务管理器）服务

### 5.1 创建专用用户和数据目录

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin macro-data
sudo install -d -o macro-data -g macro-data -m 0700 \
  /mnt/data/macro-data-platform/xtquant
sudo install -d -o root -g root -m 0700 /etc/macro-data-platform
```

如果用户已经存在，先用 `getent passwd macro-data` 核对，不要重复创建。数据目录位于本机 NVMe；
不要把 data centre cache 写到 `/archive` 备份盘。

### 5.2 创建 root-only EnvironmentFile

```bash
sudo install -o root -g root -m 0600 \
  deploy/systemd/xtquant.env.example \
  /etc/macro-data-platform/xtquant.env
sudoedit /etc/macro-data-platform/xtquant.env
```

编辑时：

- `XTQUANT_TOKEN`：填新 token。
- `XTQUANT_BIND_ADDRESS`：填第 4 节确认的 K3s 节点 InternalIP，不能是 `0.0.0.0`。
- `XTQUANT_PORT=58615`。
- `XTQUANT_DATA_HOME=/mnt/data/macro-data-platform/xtquant`。
- `XTQUANT_INIT_MARKETS=HK`；不要为了“可能有用”加载所有市场。
- `PYTHONPATH` 保持为已经验证的 versioned vendor package；升级 SDK 必须走单独验证。

禁止执行 `systemctl show ... --property=Environment`、`cat /proc/<pid>/environ` 或把文件内容粘贴到终端
记录。只检查权限：

```bash
sudo stat -c '%U %G %a %n' /etc/macro-data-platform/xtquant.env
```

预期为 `root root 600`。

### 5.3 安装并启动 unit

```bash
sudo install -o root -g root -m 0644 \
  deploy/systemd/xtquant-data-center.service \
  /etc/systemd/system/xtquant-data-center.service
sudo systemd-analyze verify /etc/systemd/system/xtquant-data-center.service
sudo systemctl daemon-reload
sudo systemctl enable --now xtquant-data-center.service
sudo systemctl status xtquant-data-center.service --no-pager
sudo ss -ltnp | rg ':58615\b'
```

unit 的启动后检查最多等待 180 秒。服务主循环每 10 秒自检监听；监听消失时进程以失败状态退出，
systemd 在 5 秒后重启。端口被其他进程占用时不会 kill 对方，而是启动失败。

只查看最少日志，不复制 native SDK 的大段连接信息：

```bash
sudo journalctl -u xtquant-data-center.service -n 100 --no-pager
```

若日志意外出现 token，立即停止服务、再次轮换并修复日志边界。

## 6. 进程与主机重启验收

维护窗口内执行：

```bash
sudo systemctl kill --signal=SIGKILL xtquant-data-center.service
sleep 10
sudo systemctl is-active xtquant-data-center.service
sudo systemctl show xtquant-data-center.service \
  --property=NRestarts --property=ActiveState --property=SubState
```

预期服务自动恢复且 `NRestarts` 增加。然后安排宿主机重启，确认 unit 随 `multi-user.target` 自动启动。
不要在 07:50–08:30 日报窗口执行故障演练。

## 7. 确认 HSI/HSTECH 的真实 XtQuant symbol

服务健康后从宿主机执行探测。命令不需要 token；它只连接已运行的 data centre：

```bash
node_ip="$(sudo k3s kubectl get node -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
uv run macro-data-xtquant-probe --host "$node_ip" --port 58615
```

探测器自动选择 HK index-like sector，扫描 instrument metadata，并且仅在以下条件同时满足时返回 0：

- 恰好一个名称严格对应“恒生指数 / Hang Seng Index”；
- 恰好一个名称严格对应“恒生科技指数 / Hang Seng TECH Index”。

输出只包含目标名、source symbol、instrument name、exchange ID 和 product type，以及扫描数量；不会
输出 sector 全列表、价格、成交量、K 线或原始响应。若自动 sector 选择不完整，可由 Beast 负责人先
安全查看 sector 名称，再显式传一个或多个 `--sector`；仍不得手填或猜 symbol。

确认结果需由第二人复核名称和产品类型。随后在 macro platform 新 PR 中：

1. 扩展 `HK_XTQUANT_DEFAULT_INSTRUMENTS` 与稳定 canonical identity。
2. 添加 provider contract、日期精度、freshness 和质量门禁测试。
3. 更新 `.env.example`/K8s ConfigMap 的 `HK_XTQUANT_SYMBOLS`。
4. live smoke 只输出成功布尔、source symbol、日期和行数，不输出 paid payload。

## 8. 回滚与故障处理

- 新 token 鉴权失败：停止新服务，核对供应商授权；旧 token 若尚未撤销，也不得重新写回 Git。只有项目
  负责人可以决定短时回退，且必须记录时间边界。
- SDK 升级失败：恢复上一条 versioned `PYTHONPATH`，`daemon-reload` 后重启；不修改 token。
- 端口冲突：用 `ss`/`systemctl status` 找 owner；不执行按端口 kill。
- worker 不可达：先确认 host self-check，再核对 bind address、Pod CIDR、防火墙和 NetworkPolicy。
- probe incomplete/ambiguous：不扩 allowlist，交由 Beast 负责人复核 entitlement metadata。

## 9. #50 完成定义

以下证据全部完成后才能关闭：

- 供应商侧旧 token 已撤销，Beast 当前分支非空 tracked token 数为 0。
- EnvironmentFile 权限为 `0600`，unit 使用专用用户；Git/CI/log 无真实凭据。
- `58615` 只有 worker Pod 可达，LAN/其他 Pod 不可达。
- SIGKILL 和宿主机重启后服务自动恢复。
- HSI/HSTECH symbol 由 metadata 唯一确认并进入 allowlist/contract/quality gate。
- 连续两个报告日使用 live XtQuant 完成，不回退 fixture，不重复投递。

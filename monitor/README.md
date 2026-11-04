# Homelab Monitor — 家庭设备状态看板

自建的轻量监控看板：各设备定时把自己的温度、负载、内存、磁盘报上来，网页端集中查看。
无第三方依赖，镜像 49MB，数据存本地 SQLite。

> **配置**：全部走 `.env`。先 `cp .env.example .env` 再填自己的地址与令牌——仓库里只有模板，没有任何真实值。

## 访问

- 内网（**可看面板，不需密码**）：http://192.168.X.32:8930
- 公网（**仅上报，看不了面板**）：http://YOUR_DDNS_DOMAIN:18930

服务端按来源 IP 分流：

| 来源 | 可访问路径 |
|---|---|
| 内网 / 受信网段（`TRUSTED_NETS`） | 全部，**免认证** |
| 公网（含 `UNTRUSTED_SRC`） | 仅 `/api/report`、`/api/health`、`/agent/install.sh`、`/agent/fnos-monitor-report.py` |

公网访问首页或 `/api/devices` 一律返回 `403 forbidden`。
上报接口 `/api/report` 用设备令牌鉴权，不需要 Basic Auth。

### 内网免密（2026-10-30 起）

`require_auth()` 对受信来源直接放行，浏览器不再弹登录框。
`AUTH_USER` / `AUTH_PASS` 仍留在 `.env` 里，作为公网侧万一被放行时的兜底，平时不参与校验。

### 必看：路由器源地址转换会伪造内网来源

家里路由器做 18930 端口映射时开了**源地址转换**，公网请求到达容器时
源 IP 变成了网关 `192.168.X.1`，被 `TRUSTED_NETS` 误判成内网，**导致公网能看面板**。
实测确认方式：公网请求 `/api/health` 返回 `"src": "192.168.X.1"`，

所以 `.env` 里必须有：

```
UNTRUSTED_SRC=192.168.X.1
```

这条把网关地址排除出受信范围，公网恢复 `403`。若路由器 SNAT 用的是别的地址，
按同样方式加进这个逗号分隔列表。

> 更彻底的做法是**直接关掉 18930 这个公网映射**——现在手机、FN7567、本机
> 全部走 WireGuard，上报也走隧道，公网入口已经没有实际用途了。

> `TRUST_PROXY` 默认 `0`，即**不信任** `X-Forwarded-For`，防止伪造内网 IP 绕过限制。
> 只有当前面挂了反向代理、且代理会覆盖该头时才改成 `1`。

## 已接入设备

| 设备 | ID | 地址 | 说明 |
|---|---|---|---|
| FN-Lab | `fnlab` | 192.168.X.32 | 中心节点，QEMU 虚拟机，无温度传感器（主指标自动切 CPU 使用率） |
| FN7567 | `fn7567` | 192.168.Y.100 | 游匣 7567 实机，走 WireGuard 隧道上报 |
| PVE-宿主 | `pve` | 192.168.X.10 | Proxmox VE 9.1，FN-Lab 的宿主机，附带虚机/容器状态 |

## 部署位置

中心节点在 **FN-Lab（192.168.X.32）**：

```
/vol1/docker/monitor/
├── docker-compose.yml
├── Dockerfile
├── .env                      # 令牌与设备注册（含密钥，勿外传）
├── app/
│   ├── server.py             # 后端：标准库 HTTP + SQLite + 来源分流
│   └── static/
│       ├── index.html        # 前端：自绘 Canvas 图表
│       └── agent/            # 采集端包，经 /agent/* 对外分发
│           ├── install.sh
│           └── fnos-monitor-report.py
├── agent/                    # 源码副本（改这里，再同步到 static/agent）
└── data/monitor.db           # 数据（挂载进容器）
```

> 改动采集端时，`agent/` 下改完要复制一份到 `app/static/agent/`，否则设备下载到的还是旧版。

`app/` 以只读方式挂载进容器，**改完页面文件不需要重建镜像**，刷新浏览器即可生效。

## 常用命令

```bash
cd /vol1/docker/monitor

docker compose ps                 # 状态
docker compose logs -f --tail 50  # 日志
docker compose restart            # 重启
docker compose up -d --force-recreate   # 改 compose 后重建
```

## 采集端

每台设备装一个上报脚本，由 systemd timer 每 5 分钟触发：
| 文件 | 作用 |
|---|---|
| `/usr/local/bin/fnos-monitor-report.py` | 采集脚本 |
| `/etc/fnos-monitor.env` | 上报地址 + 设备令牌 |
| `/etc/systemd/system/fnos-monitor-report.service` | oneshot 服务 |
| `/etc/systemd/system/fnos-monitor-report.timer` | 5 分钟定时器 |

查看状态：

```bash
systemctl list-timers fnos-monitor-report.timer
journalctl -u fnos-monitor-report.service -n 30 --no-pager
python3 /usr/local/bin/fnos-monitor-report.py --dry-run   # 只打印不发送
```

### Proxmox VE 宿主机

采集端会自动识别 PVE（检测 `pvesh`），无需额外参数：

- 用 `pvesh get /cluster/resources` 汇总 VM/CT 数量与运行状态、已分配内存与 vCPU
- 用 `lvs` 读 **LVM thin pool**（`pve/data`）使用率 —— `local-lvm` 不挂载，`df` 看不到它
  （只取 thin pool 本身，`V` 开头的 thin 卷是各虚拟磁盘，跳过）
- 用 `MOUNTS=/` 覆盖默认挂载点，避免去查不存在的 `/vol1`
- 主板温度取 `it8613` 这类 Super I/O 芯片

面板上这台设备的卡片会把 PCH 那格换成「虚机 5/5」，详情面板多一块「虚拟机 / 容器」列表。

安装示例（PVE 上以 root 执行）：

```bash
curl -fsSL http://192.168.X.32:8930/agent/install.sh | \
  SERVER=http://192.168.X.32:8930 TOKEN=<令牌> NAME=PVE-宿主 \
  MODEL="Proxmox VE 9.1 / Pentium Gold 8505" MOUNTS=/ sh
```

## 新增一台设备（一条命令）

在本机（能访问中心的机器）先登记，拿到令牌和现成的安装命令：

```bash
python scripts/add_device.py 设备名 设备ID "型号描述"
# 例：python scripts/add_device.py 客厅小主机 living "N100 / 16G"
```

输出会直接给出可复制的安装命令。到目标设备上以 root 执行：

```bash
curl -fsSL http://YOUR_DDNS_DOMAIN:18930/agent/install.sh | \
  sudo SERVER=http://YOUR_DDNS_DOMAIN:18930 TOKEN=<令牌> NAME=客厅小主机 MODEL="N100 / 16G" sh
```

- **同一内网**：`SERVER` 用 `http://192.168.X.32:8930`
- **跨网段 / 外网**：`SERVER` 用 `http://YOUR_DDNS_DOMAIN:18930`（公网只放行上报）

脚本会自动完成 4 步：下载采集端 → 写配置 → 建 systemd service + timer → 立即上报一次。
支持 `INTERVAL=2min` 调整频率；无 systemd 的环境自动退回 crontab。

重复执行是幂等的，不会产生两套定时任务。

手动登记（不经脚本）：

```bash
curl -X POST http://192.168.X.32:8930/api/devices \
  -u admin:<AUTH_PASS> \
  -H "X-Admin-Token: <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"id":"nas3","name":"NAS-3","token":"<openssl rand -hex 16>"}'
```

## 卸载采集端

```bash
systemctl disable --now fnos-monitor-report.timer
rm -f /etc/systemd/system/fnos-monitor-report.service \
      /etc/systemd/system/fnos-monitor-report.timer \
      /usr/local/bin/fnos-monitor-report.py /etc/fnos-monitor.env
systemctl daemon-reload
```

## 接口

| 方法 | 路径 | 公网 | 说明 |
|---|---|---|---|
| POST | `/api/report` | ✅ | 设备上报，头 `X-Device-Token` |
| GET | `/api/health` | ✅ | 健康检查，返回 `trusted` 字段 |
| GET | `/agent/install.sh` | ✅ | 采集端安装脚本 |
| GET | `/agent/fnos-monitor-report.py` | ✅ | 采集端主体 |
| GET | `/api/devices` | ❌ 403 | 设备列表 + 最新一次数据 |
| GET | `/api/devices/{id}/history?hours=24` | ❌ 403 | 历史曲线数据 |
| GET | `/api/devices/{id}/latest` | ❌ 403 | 单设备最新 |
| POST | `/api/devices` | ❌ 403 | 登记设备，需 `X-Admin-Token` |

## 数据保留

默认保留 90 天（`.env` 的 `RETENTION_DAYS`），每 6 小时自动清理一次过期记录。
按 5 分钟一次、两台设备算，90 天约 5 万条，占用几十 MB。

## 指标说明

- **温度**来自 `/sys/class/hwmon`，按传感器名归类：`coretemp`/`k10temp`→CPU、`nvme`→硬盘、`pch`→南桥、`iwlwifi`→无线、`it87xx`/`nct`/`dell_smm`→主板
- **磁盘**默认查 `/`、`/vol1`、`/vol2`，可用 `MOUNTS=/data,/srv` 覆盖；额外自动附上 LVM thin pool
- **虚拟机没有温度传感器**，这类设备的卡片主指标会自动切换成 CPU 使用率
- 温度配色：<60°C 绿、60–75°C 黄、≥75°C 红
- CPU 使用率采样 1 秒（读两次 `/proc/stat` 求差）

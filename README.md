# Homelab 全屋网络监控面板

把自己家里的网络、虚拟化、存储、无线、智能家居全部塞进一块屏：**一眼看出哪里不对**，而不是逐台设备翻卡片。

整套东西跑在一台小型 fnOS / Linux 主机上，纯 Python 标准库 + 原生 JS，**零第三方运行时依赖**，容器镜像约 60MB。

---

## 目录结构

```
.
├── noc/                        新版「全屋状态大屏」（主项目）
│   ├── app/
│   │   ├── server.py           HTTP 服务 + SSE 推送 + 采集调度
│   │   ├── state.py            快照聚合 / 历史环形缓冲 / 事件流 / 健康分
│   │   ├── config.py           配置加载（环境变量 + config.local.json）
│   │   ├── assets.py           静态资产表（从本地配置读取）
│   │   ├── collectors/         七路采集器
│   │   │   ├── ikuai.py        爱快路由 API
│   │   │   ├── pve.py          Proxmox VE API
│   │   │   ├── switch.py       TP-Link 系网页管理的 PoE 交换机
│   │   │   ├── miwifi.py       小米路由器（AP 模式）
│   │   │   ├── ha.py           Home Assistant 实体
│   │   │   ├── localnet.py     本机内核读数（/proc）
│   │   │   └── pinger.py       TCP RTT 探测
│   │   └── static/             单页前端（index.html / style.css / app.js）
│   ├── agent/
│   │   ├── nas_net_agent.py    NAS 侧采集端（补二层流量盲区）
│   │   └── compose-create.example.json
│   ├── config.example.json     站点配置模板（复制为 config.local.json）
│   ├── .env.example            凭据模板
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── README.md               子系统详细文档（采集协议的坑都记在这里）
├── monitor/                    旧版「设备温度/负载看板」（仍在用）
│   ├── app/                    服务端 + 静态页
│   ├── agent/                  采集端 + 一键安装脚本
│   └── .env.example
├── scripts/                    运维脚本（部署、探测、WireGuard、fnOS 调优）
│   └── _common.py              统一从 .env 读连接参数
├── tests/                      单元测试 + 前端渲染桩测试
├── .env.example                运维脚本的凭据模板
├── .gitignore
└── README.md
```

---

## 功能列表

### 新版大屏（`noc/`）

| 模块 | 内容 |
|---|---|
| **健康分** | 0–100 综合评分，异常项可点开看清单；红/琥珀/绿三档 |
| **物理拓扑** | 光猫 → 主路由 → 交换机 → AP / PVE / NAS，**链路流光速度 = 该端口实测流量** |
| **端口级流量** | 交换机每个口的 link 速率、实时收发、PoE 功率；本机降噪后不会误报断链 |
| **无线覆盖** | 每台 AP 的 SSID、**真实信道**、在线数、客户端 MAC 归属到哪个 AP |
| **终端墙** | 全屋终端按分类铺开，点击看 IP / MAC / 厂商 / 接入 AP / 累计流量 |
| **算力水位** | PVE 宿主机 CPU/内存/rootfs、每台 VM/LXC 明细与网络差分 |
| **环境与智能家居** | 室内外温湿度、29 盏灯的开关与亮度（按房间分组）、窗帘、人体存在、空调 |
| **事件流** | 上下线、链路翻转、灯光开关，都带时间去抖与重复抑制 |
| **二层流量** | NAS 侧采集端上报的网卡总吞吐，扣掉网关侧外网流量 = 内网直连流量 |
| **明暗主题** | 跟随系统 / 手动切换，选择记在 localStorage，首屏不闪白 |

### 旧版看板（`monitor/`）

温度、CPU、负载、磁盘的轻量汇总，带**来源分流**（内网可看面板，公网只允许上报）与 SQLite 历史留存。

---

## 快速开始

### 环境要求

- Linux（推荐 fnOS / Debian 系）或任何能跑 Docker 的机器
- Docker + Docker Compose
- Python 3.9+（只有跑 `scripts/` 时才需要，容器内自带）
- 想跑脚本的话：`pip install paramiko`

### 1. 取代码

```bash
git clone <你的仓库地址> homelab-noc
cd homelab-noc
```

### 2. 配置：三步复制

仓库里**没有任何真实地址与密码**，全部靠下面三个模板生成：

```bash
# ① 看板主配置（凭据）
cp noc/.env.example noc/.env

# ② 站点布局（设备清单、端口接线、拓扑文字）—— 这一步最容易被忘
cp noc/config.example.json noc/config.local.json

# ③ 运维脚本用（只在跑 scripts/ 时需要）
cp .env.example .env
```

然后编辑这两个文件：

**`noc/.env`** —— 只管"连什么、用什么账号"：

```ini
IKUAI_URL=https://192.168.X.1
IKUAI_USER=admin
IKUAI_PASS=你的路由器密码

PVE_URL=https://192.168.X.10:8006
PVE_USER=root@pam
PVE_PASS=你的PVE密码

SWITCH_URL=http://192.168.X.3
SWITCH_USER=admin
SWITCH_PASS=你的交换机密码

MIWIFI_USER=admin
MIWIFI_PASS=你的AP密码

HA_URL=http://192.168.X.20:8123
HA_USER=你的HA账号
HA_PASS=你的HA密码

NAS_REPORT_TOKEN=随便生成一串十六进制
NAS_IP=192.168.X.30
```

> 令牌生成：`python -c "import secrets;print(secrets.token_hex(16))"`

**`noc/config.local.json`** —— 只管"家里长什么样"：

- `devices`：IP → 名称 / 房间 / 图标 / 分类
- `switch.ports`：交换机每个口接的是谁
- `wifi.hosts`：每台 AP 的地址
- `probes`：要测延迟的节点
- `topology`：拓扑节点上显示的文字（可以写自己的 IP 末位）

**任何一项没配都不会崩**：缺 `config.local.json` 时终端墙为空、拓扑用通用标签；缺 `.env` 里某个地址时对应采集器会显示"未配置"而不是去连一个瞎猜的地址。

### 3. 启动

```bash
cd noc
mkdir -p data
docker compose up -d --build
```

构建时如果镜像源报 401，用传统构建器绕开：

```bash
DOCKER_BUILDKIT=0 docker compose build
```

### 4. 验证

```bash
# 服务活着吗
curl -s http://127.0.0.1:8940/api/health

# 看各采集器有没有报错
curl -s http://127.0.0.1:8940/api/snapshot | python3 -c "import json,sys; print(json.load(sys.stdin).get('errors'))"
```

浏览器打开 `http://<主机IP>:8940` 即可。

### 5. 更新（改完代码之后）

```bash
# 前端（app/static 是只读挂载，改完刷新浏览器就生效）
# 后端（Python）
docker compose restart
```

或者用仓库自带的增量部署脚本（推代码 + 传配置 + 重建容器一条龙）：

```bash
python scripts/deploy_noc.py
```

---

## 运维脚本

所有脚本的连接参数都从根目录 `.env` 读，缺项会直接告诉你缺哪个变量，不会静默失败。

| 脚本 | 作用 |
|---|---|
| `scripts/deploy_noc.py` | 增量部署新版大屏到中心节点 |
| `scripts/add_device.py` | 在旧看板登记新设备并打印安装命令 |
| `scripts/pve_probe.py` | 勘察 PVE 宿主机环境 |
| `scripts/pve_install.py` | 把采集端装到 PVE 宿主机 |
| `scripts/fnos_lid_disable.py` | fnOS 合盖不休眠 |
| `scripts/fnos_check_failed.py` | 查 fnOS 开机 failed 的服务 |
| `scripts/wg_setup.py` | 重建 WireGuard 服务端 |
| `scripts/wg_add_pc.py` | 给新客户端加一个 peer |

> WireGuard 的私钥、二维码、`*.conf` 全部在 `wireguard/` 下，**已整体 gitignore**，永远不要提交。

---

## 测试

不需要 pytest，也不需要真实设备——测试用合成数据：

```bash
python tests/test_devices.py    # 终端上下线降噪（9 组场景）
python tests/test_debounce.py   # 链路事件去抖
python tests/test_settle.py     # 交换机读数抖动复核

# 前端渲染：给一份快照 JSON，用 DOM 桩跑 app.js
node tests/render_test.js <快照.json> noc/app/static/app.js
```

`test_devices.py` 会自己注入合成资产表，地址前缀取自 `config.LAN_PREFIX`，**换任何内网段都能跑**。

---

## 安全设计

这个仓库按"公开可发布"的标准整理，规则如下：

| 类别 | 处理方式 |
|---|---|
| 密码 / 令牌 | 只存在于 `.env`（gitignore），仓库里只有 `*.example` 模板 |
| 内网地址 / 设备清单 | 只存在于 `config.local.json`（gitignore），模板里是 `192.168.X.X` |
| WireGuard 私钥 | 整个 `wireguard/` 目录 gitignore |
| 运行数据 | `data/`、`*.db`、`*.log` gitignore |
| 前端 | 拓扑文字由后端下发（`site.topology`），代码里没有 IP |

提交前建议自查一遍：

```bash
git status --short          # 有没有意外的新文件
git diff --cached --stat    # 暂存区都改了什么
git grep -nE "192\.168\.[0-9]+\.|password\s*=\s*['\"][^'\"]" -- .  # 兜底扫一遍
```

---

## 后续更新流程

1. 改代码 → 本地跑 `tests/` 三个 Python 套件，确保没回归
2. `python scripts/deploy_noc.py` 部署到实际设备，打开面板确认数据正常
3. `git add -p` 逐块看改动，**确认没有 `.env`、没有真实 IP** 再 `git commit`
4. 推送前 `git status` 复核一次

提交信息建议用 `模块: 做了什么` 的形式，例如：

```
noc/采集: 交换机 spd_act 读数抖动时用流量交叉校验
monitor: 补来源分流，公网只放行上报接口
docs: 脱敏 README，补配置模板
```

---

## 已知边界

- **单网段大二层下，网关看不到同网段互访流量**。所以服务器/NAS 会被网关周期性"踢出"在线表，本项目靠 TCP 探测交叉验证 + 时间去抖来消除误报；不是设备真掉线。
- **同一 AP 下的设备互访**走 AP 内部直转，交换机和网关都统计不到，只能靠 NAS 侧采集端补。
- 部分厂商设备的网页接口字段不稳定（比如某型号交换机的 `spd_act` 约 1/6 概率读 0），采集器已做交叉校验，细节见 `noc/README.md`。

---

## 许可

个人自用项目，随便看随便抄。

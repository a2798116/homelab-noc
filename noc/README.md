# Homelab NOC — 全屋状态大屏

一眼看全屋：网络拓扑、**交换机端口级实时流量**、无线覆盖、终端在线、算力水位、存储压力、环境状态、异常事件。

部署在一台常开的 Linux / fnOS 主机上（容器 `homelab-noc`，默认端口 `8940`），浏览器直接访问即可，无需额外前端服务。

---

## 一、它回答什么问题

旧看板是"每个设备怎么样"的卡片列表；这个是"**哪里不对**"的态势面板。

信息分三档：

| 层次 | 位置 | 看什么 |
|---|---|---|
| 远景 | 顶栏 + 状态带 | 全屋健康分、室内外温湿度、灯光/空调/门窗、有没有红点 |
| 中景 | 中央拓扑 + 左右侧栏 | 哪条链路在跑流量（端口级实测）、无线几台在线、哪个节点延迟高 |
| 近景 | 终端墙点击 / VM 列表 | 单设备 IP、MAC、接入哪个 AP、累计流量、连接数 |

布局：

```
顶栏 brand + 健康分 + 外网 + 在线 + 连接 + 吞吐 + 时钟
状态带 室内温湿度 · 室外 · 灯光 · 空调 · 门窗 · 无线 · 在家 · HA
左栏   外网链路 / 交换机端口 / 节点延迟
中央   物理拓扑（链路流光速度 = 该端口实测流量）
右栏   PVE+存储水位 / 无线覆盖 / 事件流
底栏   全屋终端墙（按分类分组，点击看详情）
```

## 二、数据源（六路）

| 源 | 接口 | 频率 | 拿到什么 |
|---|---|---|---|
| iKuai 主路由 | `POST https://192.168.X.1/Action/call` | 2s | CPU/内存/在线数/连接数/实时流速 |
| iKuai 接口 | `monitor_iface` `iface_stream` | 2s | wan1 / lan1 实时字节流、物理口速率链路 |
| iKuai 终端 | `monitor_lanip` | 5s | 全屋终端列表（实测 33–35 台） |
| PVE | `GET https://192.168.X.10:8006/api2/json/...` | 5s | 宿主机 CPU/内存/rootfs、VM + LXC 明细、每虚拟机网络差分 |
| **水星 SE106P** | `GET http://192.168.X.3/MainRpm.htm` | 3s | **每端口 link 速率 / 实时收发 / PoE 功率** |
| **小米 AP ×2** | `/api/misystem/topo_graph`、`device_list`、`wifi_detail_all` | 10s | SSID、信道、每 AP 在线数、无线客户端 MAC 归属 |
| **Home Assistant** | `GET http://192.168.X.20:8123/api/states` | 15s | 室温湿度、室外天气、空调、灯光、门窗、在家状态、实体健康率 |
| 本机内核 | `/proc/net/dev` | 2s | FN-Lab 网卡吞吐、负载、存储水位 |
| TCP 探测 | socket connect | 10s | 8 个关键节点的 RTT / 抖动 / 丢包 |

全部 **零第三方依赖**（Python 标准库 + 原生 JS），镜像约 60MB。

### 三个设备各自的认证坑（都是实测踩出来的）

| 设备 | 坑 |
|---|---|
| 水星 SE106P | 密码不能明文/md5/base64，要走页面里 `cryp_new.js` 的 `securityEncode`（函数名叫 `hex_md5` 但实际是异或映射表）。表长 255 字符，抄错一个字符就 `logonInfo=1` 登录失败。 |
| 小米 AP | `POST /cgi-bin/luci/api/xqsystem/login` 明文 `logtype=1` 即可拿 stok；真实信道在 `channelInfo.channel`，顶层 `channel` 字段恒为 `"0"`（配置值=自动）。子 AP 自己报不出在线数，要从**主 AP 的 `topo_graph.leafs`** 反查。 |
| Home Assistant | `/auth/login_flow` 的 `handler` 必须是 `["homeassistant", null]`，写成 `["homeassistant","login_flow"]` 直接 **500**。`/auth/token` 必须 **form 编码**，JSON body 会被读成空 → `unsupported_grant_type`。access_token 30 分钟过期，用 refresh_token 静默续期并落盘 `/data/ha_token.json`。 |

## 三、部署

```bash
# 首次：先准备两份配置（仓库里只有模板，没有真实值）
cp .env.example .env                    # 凭据
cp config.example.json config.local.json  # 站点布局

# 起容器
mkdir -p data
docker compose up -d --build

# 改前端（app/ 已只读挂载，刷新浏览器即可，无需重启）
# 改后端（Python）
docker compose restart
```

构建时若镜像源报 401，用 `DOCKER_BUILDKIT=0 docker compose build`。

### 目录

```
noc/
├── app/
│   ├── server.py            HTTP + SSE + 采集调度
│   ├── state.py             快照聚合 / 历史 / 事件 / 健康分
│   ├── assets.py            静态资产表（读 config.local.json）
│   ├── config.py            配置：环境变量 + config.local.json
│   ├── collectors/
│   │   ├── ikuai.py         爱快 API
│   │   ├── pve.py           Proxmox API
│   │   ├── switch.py        交换机端口统计
│   │   ├── miwifi.py        小米 AP 无线侧
│   │   ├── ha.py            Home Assistant 实体
│   │   ├── localnet.py      本机内核读数
│   │   └── pinger.py        TCP RTT 探测
│   └── static/              index.html / style.css / app.js
├── agent/
│   ├── nas_net_agent.py     NAS 侧采集端
│   └── compose-create.example.json
├── config.example.json      站点配置模板
├── config.local.json        站点配置（gitignore，部署时传 data/）
├── .env.example             凭据模板
├── data/                    HA token 缓存 + config.local.json（持久化，勿删）
├── docker-compose.yml
├── Dockerfile
└── .env                     凭据（gitignore）
```

### 配置：两份文件分工

**`.env` —— 连什么、用什么账号**（模板 `.env.example`）

```
IKUAI_URL / IKUAI_USER / IKUAI_PASS
PVE_URL / PVE_USER / PVE_PASS / PVE_NODE
SWITCH_URL / SWITCH_USER / SWITCH_PASS
MIWIFI_USER / MIWIFI_PASS            # 多台 AP 共用一组密码
HA_URL / HA_USER / HA_PASS / HA_TOKEN_FILE
NAS_REPORT_TOKEN / NAS_IP / NAS_STALE
PORT=8940
POLL_FAST=2  POLL_DEVICES=5  POLL_PVE=5  POLL_PING=10
POLL_SWITCH=3  POLL_MIWIFI=10  POLL_HA=15
DEV_GRACE=25  DEV_REPEAT=180
```

**`config.local.json` —— 家里长什么样**（模板 `config.example.json`）

| 段 | 作用 |
|---|---|
| `site` | 站点名、网段（决定探活补位的范围）、NAS 地址 |
| `switch.ports` | 交换机每个口接谁（端口号 → 角色 + 拓扑节点） |
| `wifi.hosts` | 每台 AP 的地址、显示名、型号、对应拓扑节点 |
| `probes` | 要测 RTT 的节点（key 与前端 `NODES` 对应） |
| `devices` | 静态资产表：IP → 名称 / 房间 / 图标 / 分类 |
| `topology` | 拓扑节点上的显示文字（可写自己的 IP 末位） |

两份文件缺失都不会让服务起不来：`config.local.json` 缺失时终端墙为空、拓扑用通用标签；`.env` 里某个地址缺失时，对应采集器直接报「未配置」并跳过，不会去连瞎猜的地址。

`config.local.json` 在容器里读 `/data/config.local.json`（对应 `./data:/data`），也可以用 `NOC_LOCAL_CONFIG` 指定路径。

## 四、接口

| 路径 | 说明 |
|---|---|
| `GET /` | 大屏页面 |
| `GET /api/snapshot` | 全量快照（约 33KB） |
| `GET /api/stream` | SSE，1.5s 一帧 |
| `GET /api/history?minutes=30` | 历史曲线（内存环形缓冲，最长 1 小时，含各端口速率） |
| `GET /api/events?limit=50` | 事件流 |
| `GET /api/health` | 存活检查 |

## 五、二层流量的边界（接入交换机后的修订版）

之前结论是"手机 ↔ NAS 的流量完全测不到"，**拿到水星 SE106P 的端口统计后要修正**：

| 流量路径 | 能否计量 | 靠什么 |
|---|---|---|
| 子AP 设备 ↔ NAS / 主AP 设备 | ✅ | 交换机 **口1 / 口2** 的 rx/tx 差值 |
| 任意设备 ↔ 外网 | ✅ | iKuai wan1 + 交换机口5 |
| PVE ↔ 全屋 | ✅ | 交换机 **口4**（发卡弯回程） |
| **同一 AP 下 手机 ↔ NAS** | ❌ | 在 AP 内部直转，不上交换机 |

所以界面拓扑上"二层旁路"那条灰虚线代表的是**最后一种**：同 AP 内部直连。
跨 AP 的流量现在是实测值，光点速度和端口列表里的数字都来自设备。

**要补最后这块**，只能在极空间 Z4Pro 上跑一个读 `/proc/net/dev` 的上报容器
（`NAS 网卡总吞吐 − iKuai 统计的 .30 外网流量 = 同 AP 二层直连流量`）。

## 六、健康分规则

基础 100 分，扣分项：

- WAN 拨号异常 −45
- 关键节点探测失败 −12 / 个
- 路由 CPU > 90% −8，内存 > 92% −10
- PVE 内存 > 92% −10，系统盘 > 90% −8
- 存储池 > 88% −6
- 交换机不可达 −10；**挂了设备的口断开 −9 / 个**
- 小米 AP 不可达 −10；单台 AP 掉线 −8
- HA 采集失败 −6；可用实体率 < 35% −8

≥90 绿，70–89 琥珀，<70 红。正常时安静，异常时顶栏变红并在事件流留痕。

## 七、后续可扩展

- 极空间 NAS agent（补同 AP 二层直连流量）
- 事件持久化到 SQLite，支持 7 天回溯
- HA 场景反向控制（点灯、调空调）
- 手机端竖屏布局（当前 `@media` 已做两档降级）

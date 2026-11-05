# -*- coding: utf-8 -*-
"""配置

三层来源，优先级从高到低：

1. 环境变量（容器里由 docker-compose 的 env_file 注入 .env）
2. 本地私有配置文件 config.local.json —— 站点相关的拓扑与资产表放这里，
   该文件已被 .gitignore 忽略，不会进仓库
3. 代码内置的通用默认值（不含任何真实地址）

模板见 config.example.json / .env.example。
"""
import ipaddress
import json
import os

# ---------------------------------------------------------------- 版本
# 容器镜像只 COPY app/，仓库根目录的 VERSION 不在镜像里，所以保留兜底常量。
# 发布时只需改 VERSION 与 CHANGELOG.md，容器重启后 /api/health 会跟着变。
APP_VERSION = "2.2.1"
try:
    _ver_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "VERSION",
    )
    with open(_ver_file, "r", encoding="utf-8") as _vf:
        APP_VERSION = _vf.read().strip() or APP_VERSION
except OSError:
    pass

# ---------------------------------------------------------------- 配置文件定位


def _load_dotenv():
    """把 .env 读进环境变量。已存在的环境变量优先，不会被覆盖。"""
    for p in (os.getenv("NOC_ENV_FILE"), "/data/.env", "../.env", ".env"):
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError:
            pass
        break


def _load_local():
    """读 config.local.json（站点私有配置）。

    找不到就返回空 dict —— 程序仍然启动，只是没有资产表与探测目标。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (
        os.getenv("NOC_LOCAL_CONFIG"),
        "/data/config.local.json",
        os.path.join(here, "config.local.json"),
        os.path.join(here, "..", "config.local.json"),
        "config.local.json",
    ):
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            # 格式写错不要拖垮整个服务，降级成"没有本地配置"
            pass
        break
    return {}


_load_dotenv()
LOCAL = _load_local()


def _sec(name, default=None):
    """取本地配置的某个段，缺失或类型不对就返回 default"""
    v = LOCAL.get(name)
    if v is None:
        return default if default is not None else {}
    return v


def _env(*names, default=""):
    for n in names:
        v = os.getenv(n)
        if v not in (None, ""):
            return v
    return default


# ---------------------------------------------------------------- 站点

SITE = _sec("site")
SITE_NAME = SITE.get("name") or "Homelab NOC"
# PVE 里的虚拟机在 PVE 后台常是 "WIFI"/"Copy-of-VM-WIFI" 这类代号，
# 这里允许在本地配置里给 vmid 起一个中文名，前端 PVE 面板直接用它
VM_NAMES = SITE.get("vm_names") or {}
# 算力页各卡片的标题：站点相关（可能是地名），写在 config.local.json 的 site.compute 里，
# 不硬编码进前端。缺失时用通用占位符。
_COMPUTE_TITLES = SITE.get("compute") or {}
PVE_TITLE = _COMPUTE_TITLES.get("pve") or "PVE"
FNOS_TITLE = _COMPUTE_TITLES.get("fnos") or "飞牛 NAS"
RIKUAI_TITLE = _COMPUTE_TITLES.get("rikuai") or "远程爱快"
LAN_CIDR = SITE.get("subnet") or "192.168.1.0/24"
try:
    LAN_PREFIX = str(ipaddress.ip_network(LAN_CIDR, strict=False).network_address)
    LAN_PREFIX = LAN_PREFIX.rsplit(".", 1)[0] + "."
except ValueError:
    LAN_PREFIX = "192.168.1."

# ---------------------------------------------------------------- iKuai 主路由
# 空值代表"未配置"，采集器会跳过并给出明确提示，而不是去连一个瞎猜的地址
IKUAI_URL = _env("IKUAI_URL", default="").rstrip("/")
IKUAI_USER = _env("IKUAI_USER", default="admin")
IKUAI_PASS = _env("IKUAI_PASS", default="")

# ---------------------------------------------------------------- PVE 宿主机
PVE_URL = _env("PVE_URL", default="").rstrip("/")
PVE_USER = _env("PVE_USER", default="root@pam")
PVE_PASS = _env("PVE_PASS", default="")
PVE_NODE = _env("PVE_NODE", default="pve")

# ---------------------------------------------------------------- 远程飞牛 NAS（SSH）
# 经 VPN 可达的远程 fnOS 主机。host 为空时采集器静默跳过。
# 算力页会多一张卡片，和 PVE 并排展示 CPU/内存/磁盘/温度。
FNOS_HOST = _env("FNOS_HOST", default="")
FNOS_PORT = int(_env("FNOS_PORT", default="22"))
FNOS_USER = _env("FNOS_USER", default="")
FNOS_PASS = _env("FNOS_PASS", default="")

# ---------------------------------------------------------------- 远程爱快（VPN 站点）
# 经 VPN 可达的远程 iKuai 路由器 API。url 为空时采集器静默跳过。
# 算力页会多一张卡片，展示远程路由器的 CPU/内存/在线数/连接数/实时速率。
RIKUAI_URL = _env("RIKUAI_URL", default="").rstrip("/")
RIKUAI_USER = _env("RIKUAI_USER", default="admin")
RIKUAI_PASS = _env("RIKUAI_PASS", default="")

# ---------------------------------------------------------------- 远程群晖备份监控（VPN 站点）
# 经 VPN 可达的远程 fnOS 上跑的群晖备份监控页面。url 为空时禁用代理。
# NOC 加一个「备份」页签，用 iframe + 反向代理把这个页面嵌进来，
# 用户从 NOC 点进去就能看远程备份状态，不用切 VPN。
BKP_URL = _env("BKP_URL", default="").rstrip("/")

# ---------------------------------------------------------------- 安防监控
# go2rtc 就在 NOC 同一台机器上，直接用回环地址反代，不必把它暴露到公网。
# 浏览器不能播 RTSP，go2rtc 负责 RTSP → fMP4 / JPEG 转封装（H265 还要转 H264）。
# 这里只配显示用的摄像头清单，RTSP 源写在 config.local.json 的 cameras 段里。
CAM_URL = _env("CAM_URL", default="http://127.0.0.1:1984").rstrip("/")
CAM_SNAP_MS = int(_env("CAM_SNAP_MS", default="2000"))   # 快照轮询间隔
CAMERAS = []
for _c in (_sec("cameras") or []):
    if not isinstance(_c, dict):
        continue
    _id = str(_c.get("id") or _c.get("src") or "")
    if _c.get("blank"):
        # 预留空槽位：网格里留个空格，不注册流也不拉画面，方便以后补摄像头
        CAMERAS.append({
            "id": _id, "title": "", "note": "", "rtsp": "",
            "src": "", "live": "", "transcode": False, "blank": True,
        })
        continue
    if not (_c.get("src") or _c.get("rtsp")):
        continue
    CAMERAS.append({
        "id": _id,
        "title": _c.get("title") or _id,
        "note": _c.get("note") or "",
        "rtsp": _c.get("rtsp") or "",
        "src": _c.get("src") or _id,           # go2rtc 里用于快照的流名
        "transcode": bool(_c.get("transcode")),
    })
    # 有 RTSP 源的流由 NOC 启动时注册进 go2rtc；H265 另注册一路转 H264 的「实时」流
    CAMERAS[-1]["live"] = (_id + "_live") if _c.get("rtsp") and _c.get("transcode") else CAMERAS[-1]["src"]

# ---------------------------------------------------------------- 交换机
SWITCH_URL = _env("SWITCH_URL", default="").rstrip("/")
SWITCH_USER = _env("SWITCH_USER", default="admin")
SWITCH_PASS = _env("SWITCH_PASS", default="")

# 端口 → 角色映射（物理接线表，来自 config.local.json）
_raw_ports = _sec("switch").get("ports") or {}
SWITCH_PORTS = {}
for _k, _v in _raw_ports.items():
    try:
        SWITCH_PORTS[int(_k)] = {
            "role": (_v or {}).get("role") or ("端口 %s" % _k),
            "node": (_v or {}).get("node"),
        }
    except (TypeError, ValueError):
        continue
if not SWITCH_PORTS:
    # 没配置时给 6 个无名端口，界面仍能显示"有 6 个口"
    SWITCH_PORTS = {i: {"role": "端口 %d" % i, "node": None} for i in range(1, 7)}

# ---------------------------------------------------------------- 小米路由器（AP 模式）
MIWIFI_HOSTS = _sec("wifi").get("hosts") or []
MIWIFI_USER = _env("MIWIFI_USER", default="admin")
MIWIFI_PASS = _env("MIWIFI_PASS", default="")

# ---------------------------------------------------------------- Home Assistant
HA_URL = _env("HA_URL", default="").rstrip("/")
HA_USER = _env("HA_USER", default="")
HA_PASS = _env("HA_PASS", default="")
# 长效访问令牌（Long-Lived Access Token）：配置了就直接用，跳过登录流程
HA_TOKEN = _env("HA_TOKEN", default="")
HA_TOKEN_FILE = _env("HA_TOKEN_FILE", default="/data/ha_token.json")

# 室内环境：房间 → 明确点名的实体。群晖/磁盘/冰箱/空调自检这些温度也叫「温度」，
# 但完全不是室温，所以「室内」这一栏只认这里列出来的实体，不再靠 device_class 瞎猜。
# 结构：[{"name":"客厅","src":"ZM1 · MQTT","entities":{"temp":"sensor.x","hum":"sensor.y","hcho":"sensor.z"}}]
HA_ENV_ROOMS = _sec("ha").get("env_rooms") or []
# 室外天气实体（不配就退回第一个有温度的 weather.*）
HA_OUTDOOR_ENTITY = _sec("ha").get("outdoor_entity") or ""
# 天气实体整块 unavailable 时的兜底（比如空调外机环境温度），按顺序取第一个有数的
HA_OUTDOOR_FALLBACK = _sec("ha").get("outdoor_fallback") or []
# 灯控面板的 switch 实体前缀（各站点设备不同，只写本地配置；不配就按实体名规则匹配）
HA_LAMP_SWITCH_PREFIX = [str(x).strip() for x in (_sec("ha").get("lamp_switch_prefix") or []) if str(x).strip()]

# ---------------------------------------------------------------- NAS 采集端
# 大二层里手机↔NAS 的流量不经网关，只有 NAS 网卡自己知道。
# 采集端容器读 /proc/net/dev 上报，NOC 再与网关统计的外网流量做差。
NAS_REPORT_TOKEN = _env("NAS_REPORT_TOKEN", default="")
NAS_IP = _env("NAS_IP", default=SITE.get("nas_ip") or "")
NAS_STALE = float(_env("NAS_STALE", default="45"))   # 超过这么久没上报判定掉线

# ---------------------------------------------------------------- 服务
PORT = int(_env("PORT", default="8940"))
HOST = _env("HOST", default="0.0.0.0")

# ---------------------------------------------------------------- 持久化
# 事件流和指标采样都落在这个 SQLite 文件里；/data 是飞牛上的容器卷，
# 重建容器不会丢。目录不可写时 store 会自己降级成"只保留内存事件"。
DB_PATH = _env("DB_PATH", default="/data/noc.db")
DB_KEEP_DAYS = float(_env("DB_KEEP_DAYS", default="90"))   # 事件保留天数
POLL_SAMPLE = float(_env("POLL_SAMPLE", default="60"))     # 指标采样间隔

# ---------------------------------------------------------------- 采集节奏（秒）
POLL_FAST = float(_env("POLL_FAST", default="2"))        # 网关流速 + 本机网卡
POLL_DEVICES = float(_env("POLL_DEVICES", default="5"))  # 终端列表
POLL_PVE = float(_env("POLL_PVE", default="5"))
POLL_FNOS = float(_env("POLL_FNOS", default="15"))   # 远程飞牛（经 VPN），不要太频繁
POLL_RIKUAI = float(_env("POLL_RIKUAI", default="15"))  # 远程爱快（经 VPN）
POLL_PING = float(_env("POLL_PING", default="10"))
POLL_SWITCH = float(_env("POLL_SWITCH", default="5"))    # 交换机端口速率（SSH 一次约 1.1s）
POLL_VPN = float(_env("POLL_VPN", default="15"))         # VPN 隧道状态
POLL_MIWIFI = float(_env("POLL_MIWIFI", default="10"))   # 小米 AP
POLL_HA = float(_env("POLL_HA", default="15"))           # Home Assistant
POLL_LANPING = float(_env("POLL_LANPING", default="15"))  # 全量 ping 终端墙设备

# ---------------------------------------------------------------- 终端上下线判定
# 网关的"在线"其实是"网关自己看见过"：同网段互访的流量不经过网关，
# 服务器/交换机这类长期不跟网关说话的设备会被周期性从在线表里踢掉。
# 所以要靠探活（ICMP / AP 客户端表）兜底，宽限期给够，别让墙上格子乱闪。
LAN_PING = _env("LAN_PING", default="1") not in ("0", "false", "False", "")
DEV_GRACE = float(_env("DEV_GRACE", default="120"))     # 消失多久才认离线（秒）
# 一次 ICMP 探通保留多久：手机 / IoT 省电时会连续几轮不回 ping，
# 单轮失败就丢证据会让在线设备被误判离线、反复播上下线。
LANPING_GRACE = float(_env("LANPING_GRACE", default="180"))
DEV_REPEAT = float(_env("DEV_REPEAT", default="180"))    # 同一设备上下线事件最小间隔

# ---------------------------------------------------------------- 历史
HISTORY_POINTS = int(_env("HISTORY_POINTS", default="1800"))  # 2s × 1800 = 1 小时

# ---------------------------------------------------------------- 探测目标
PROBE_TARGETS = []
for _t in (_sec("probes") or []):
    if isinstance(_t, dict) and _t.get("ip") and _t.get("port"):
        PROBE_TARGETS.append({
            "key": _t.get("key") or _t["ip"],
            "name": _t.get("name") or _t["ip"],
            "ip": _t["ip"],
            "port": int(_t["port"]),
        })

# ---------------------------------------------------------------- 拓扑节点显示文字
# 形如 {"ikuai": {"title": "...", "sub": "..."}}，前端启动后覆盖内置通用标签
TOPOLOGY_LABELS = _sec("topology")

# ---------------------------------------------------------------- 双 WAN 运营商
# 形如 {"wan1": {"isp": "中国联通", "name": "联通宽带", "line": "主线"}}
# 前端据此把 wan1/wan2 标成真实运营商，而不是抽象的"主线/副线"
WAN_LABELS = _sec("wan")

# ---------------------------------------------------------------- VPN
# ignore：已经废弃、不想再显示的拨号接口名（爱快上那条拨不通的 PPTP 就是它）
_vpn_sec = _sec("vpn")
VPN_IGNORE = [str(x).strip().lower() for x in (_vpn_sec.get("ignore") or []) if str(x).strip()]

# clients：拨入站点名录，键是隧道地址（10.8.8.x）。
# 爱快只给账号名和隧道 IP，站点在哪儿只有本地配置知道，所以名称一律写在这里，
# 前端点击 VPN 弹出的「一站一拓扑」就按这张表铺三张卡片。
# router/lan/gw/dev 描述站点侧内网：拨号的路由器、内网网段、网关、网段里的其他设备。
VPN_CLIENTS = {}
for _ip, _info in (_vpn_sec.get("clients") or {}).items():
    if isinstance(_info, str):
        VPN_CLIENTS[str(_ip).strip()] = {"name": _info, "short": "", "user": "",
                                         "router": "", "lan": "", "gw": "", "dev": []}
    elif isinstance(_info, dict):
        _devs = []
        for _d in (_info.get("dev") or []):
            if isinstance(_d, dict) and (_d.get("ip") or _d.get("name")):
                _devs.append({"name": _d.get("name") or _d.get("ip") or "",
                              "ip": _d.get("ip") or ""})
        VPN_CLIENTS[str(_ip).strip()] = {
            "name": _info.get("name") or str(_ip),
            "short": _info.get("short") or "",
            "user": _info.get("user") or "",
            "router": _info.get("router") or "",
            "lan": _info.get("lan") or "",
            "gw": _info.get("gw") or "",
            "dev": _devs,
        }

# ---------------------------------------------------------------- 终端分类规则
# 按主机名/厂商关键字归类，与具体站点无关，直接内置
CATEGORY_RULES = [
    {"cat": "infra", "label": "基础设施", "match": ["Proxmox", "HomeAssistant", "ZSPACE"]},
    {"cat": "net",   "label": "网络设备", "match": ["小米路由", "iyi", "Xiaomi Router"]},
    {"cat": "smart", "label": "智能家居", "match": ["Midea", "Haier", "LumiUnited", "小米摄像头",
                                                    "小米 airc", "米家", "欧普", "Yeelight", "Vela"]},
    {"cat": "media", "label": "影音数码", "match": ["小米AI音箱", "Soundbox"]},
    {"cat": "client", "label": "个人终端", "match": ["Apple", "Windows", "Android", "Linux", "iPhone"]},
]

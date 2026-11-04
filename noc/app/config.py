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
HA_TOKEN_FILE = _env("HA_TOKEN_FILE", default="/data/ha_token.json")

# ---------------------------------------------------------------- NAS 采集端
# 大二层里手机↔NAS 的流量不经网关，只有 NAS 网卡自己知道。
# 采集端容器读 /proc/net/dev 上报，NOC 再与网关统计的外网流量做差。
NAS_REPORT_TOKEN = _env("NAS_REPORT_TOKEN", default="")
NAS_IP = _env("NAS_IP", default=SITE.get("nas_ip") or "")
NAS_STALE = float(_env("NAS_STALE", default="45"))   # 超过这么久没上报判定掉线

# ---------------------------------------------------------------- 服务
PORT = int(_env("PORT", default="8940"))
HOST = _env("HOST", default="0.0.0.0")

# ---------------------------------------------------------------- 采集节奏（秒）
POLL_FAST = float(_env("POLL_FAST", default="2"))        # 网关流速 + 本机网卡
POLL_DEVICES = float(_env("POLL_DEVICES", default="5"))  # 终端列表
POLL_PVE = float(_env("POLL_PVE", default="5"))
POLL_PING = float(_env("POLL_PING", default="10"))
POLL_SWITCH = float(_env("POLL_SWITCH", default="3"))    # 交换机端口速率
POLL_MIWIFI = float(_env("POLL_MIWIFI", default="10"))   # 小米 AP
POLL_HA = float(_env("POLL_HA", default="15"))           # Home Assistant

# ---------------------------------------------------------------- 终端上下线判定
# 网关的"在线"其实是"网关自己看见过"：同网段互访的流量不经过网关，
# 服务器/交换机这类长期不跟网关说话的设备会被周期性从在线表里踢掉。
DEV_GRACE = float(_env("DEV_GRACE", default="25"))       # 消失多久才认离线（秒）
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

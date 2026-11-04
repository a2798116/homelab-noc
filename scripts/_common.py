# -*- coding: utf-8 -*-
"""运维脚本公共配置

所有连接参数一律来自环境变量；仓库根目录存在 .env 时会先载入它
（已存在的环境变量优先，不会被 .env 覆盖）。

用法：复制根目录 .env.example 为 .env，填上自己的地址与密码。
.env 已被 .gitignore 忽略，不会进仓库。

脚本里不要再出现任何明文地址或密码。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR_DIR = os.path.join(ROOT, "monitor")
NOC_DIR = os.path.join(ROOT, "noc")
AGENT_PY = os.path.join(MONITOR_DIR, "agent", "fnos-monitor-report.py")


def _load_dotenv():
    for name in (".env", ".env.local"):
        p = os.path.join(ROOT, name)
        if not os.path.isfile(p):
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
        return


_load_dotenv()


def env(name, default="", required=False, secret=False):
    """取环境变量。required=True 时缺失会直接退出并给出中文提示。"""
    v = os.environ.get(name, "") or default
    if required and not v:
        what = "密码" if secret else "配置"
        sys.exit("缺少%s项 %s —— 请在仓库根目录的 .env 里填写（模板见 .env.example）" % (what, name))
    return v


def ssh_pass():
    """SSH / sudo 密码，缺失即退出"""
    return env("SSH_PASSWORD", required=True, secret=True)


# ---------------------------------------------------------------- 设备地址
# 看板中心（fnOS）
FNLAB_HOST = env("FNLAB_HOST")
FNLAB_USER = env("FNLAB_USER", env("SSH_USER", "root"))
# Proxmox 宿主机
PVE_HOST = env("PVE_HOST")
PVE_USER = env("PVE_USER", "root")
PVE_NODE = env("PVE_NODE", "pve")
# 另一台 fnOS（跨网段那台）
FNNAS_HOST = env("FNNAS_HOST")
FNNAS_USER = env("FNNAS_USER", env("SSH_USER", "root"))
# WireGuard 服务端
WG_HOST = env("WG_HOST")
WG_USER = env("WG_USER", "root")

# ---------------------------------------------------------------- HTTP 端点
# 旧看板（monitor）中心地址与公网入口（不填则只用内网）
CENTER_URL = env("CENTER_URL")
PUBLIC_URL = env("PUBLIC_URL")

# 旧看板 Basic Auth / 管理令牌 / 设备令牌
MONITOR_AUTH_USER = env("MONITOR_AUTH_USER", "admin")
MONITOR_AUTH_PASS = env("MONITOR_AUTH_PASS")
MONITOR_ADMIN_TOKEN = env("MONITOR_ADMIN_TOKEN")
MONITOR_DEVICE_TOKEN = env("MONITOR_DEVICE_TOKEN")
# 两台采集端各自的设备令牌
MONITOR_TOKEN_CENTER = env("MONITOR_TOKEN_CENTER")
MONITOR_TOKEN_REMOTE = env("MONITOR_TOKEN_REMOTE")
MONITOR_DEVICE_NAME = env("MONITOR_DEVICE_NAME", "PVE-宿主")
MONITOR_DEVICE_MODEL = env("MONITOR_DEVICE_MODEL", "")

# ---------------------------------------------------------------- WireGuard
WG_ENDPOINT = env("WG_ENDPOINT")        # 公网 域名或 IP:端口
WG_PORT = int(env("WG_PORT", "51888"))
WG_NET = env("WG_NET", "10.88.0.0/24")  # 隧道网段
WG_SRV_IP = env("WG_SRV_IP", "10.88.0.1")
WG_PHONE_IP = env("WG_PHONE_IP", "10.88.0.2")
WG_NAS_IP = env("WG_NAS_IP", "10.88.0.3")
WG_PC_IP = env("WG_PC_IP", "10.88.0.4")
WG_LAN = env("WG_LAN")                  # 内网网段，如 10.0.0.0/24
WG_NAS_LAN = env("WG_NAS_LAN")          # 对端 NAS 的单机路由


def banner(title):
    print("=" * 64)
    print("##", title)

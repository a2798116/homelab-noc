# -*- coding: utf-8 -*-
"""H3C S5110-10P 交换机采集（Comware 5.20，SSH + CLI）

为什么走 SSH 而不是 Web：
  * 这台机器的 Web 登录页带图形验证码（/vld.bmp），没法无人值守登录；
  * SSH 一直开着，CLI 输出稳定，`display interface` 里直接带
    「Last 300 seconds input/output ... bytes/sec」，是设备自己算好的速率，
    不用我们做两次采样求差。
  * 坑：Comware 5.20 的 SSH 只提供 diffie-hellman-group14-sha1 这类老算法，
    新版 paramiko 已经删掉这些 kex，必须手工塞回 _preferred_kex，
    否则报 "no acceptable kex algorithm"。

每轮只发一条 `display interface`（一次性打印所有口），解析出：
  链路状态 / 协商速率 / 描述 / PVID / access|trunk / 收字节每秒 / 发字节每秒
版本信息（型号、序列号、uptime）单独取一次并缓存，避免每 3 秒读一遍。
"""
import re
import socket
import threading
import time

import config

try:
    import paramiko
except ImportError:                                  # 镜像里没装时给出可操作的提示
    paramiko = None

# Comware 5.20 只认这些老算法，新版 paramiko 默认已不再提供
_LEGACY_KEX = (
    "diffie-hellman-group14-sha1",
    "diffie-hellman-group1-sha1",
    "diffie-hellman-group-exchange-sha1",
)
_LEGACY_KEYS = ("ssh-rsa", "ssh-dss")
_LEGACY_CIPHERS = ("aes128-cbc", "3des-cbc", "aes256-cbc")

_PROMPT = re.compile(rb"[<\[][^<>\[\]\r\n]{1,40}[>\]]\s*$")

_lock = threading.Lock()
_conn = {"t": None, "ch": None, "ts": 0}
_ver = {"ts": 0, "data": {}}

_HDR = re.compile(r"^\s*((?:Gigabit|Ten-Gigabit)Ethernet\d+/\d+/\d+) current state: (\w+)", re.M)


def _split_host(url):
    """SWITCH_URL 支持 192.168.X.Y / ssh://user@host:port 两种写法"""
    s = (url or "").strip()
    s = re.sub(r"^ssh://", "", s)
    if "@" in s:
        s = s.split("@", 1)[1]
    host, _, port = s.partition(":")
    return host, int(port or 22)


def _drain(ch, idle=0.35, total=6.0):
    buf = b""
    t0 = time.time()
    while time.time() - t0 < total:
        if ch.recv_ready():
            buf += ch.recv(65536)
            t0 = time.time()
            continue
        time.sleep(0.03)
        if buf and not ch.recv_ready():
            time.sleep(idle)
            if not ch.recv_ready():
                break
    return buf


def _open():
    if paramiko is None:
        raise RuntimeError("容器里没有 paramiko，无法 SSH 采集交换机")
    host, port = _split_host(config.SWITCH_URL)
    sock = socket.create_connection((host, port), timeout=8)
    t = paramiko.Transport(sock)
    # 老算法插到最前面（保留新算法兜底，换设备不用改代码）
    t._preferred_kex = _LEGACY_KEX + tuple(t._preferred_kex)
    t._preferred_keys = _LEGACY_KEYS + tuple(t._preferred_keys)
    t._preferred_ciphers = _LEGACY_CIPHERS + tuple(t._preferred_ciphers)
    t.start_client(timeout=10)
    t.auth_password(config.SWITCH_USER, config.SWITCH_PASS)
    ch = t.open_session()
    ch.get_pty()
    ch.invoke_shell()
    time.sleep(0.9)
    _drain(ch, 0.4)
    ch.send("screen-length disable\n")
    _drain(ch, 0.4)
    return t, ch


def _cmd(ch, cmd, total=6.0):
    ch.send(cmd + "\n")
    time.sleep(0.12)
    buf = b""
    t0 = time.time()
    while time.time() - t0 < total:
        if ch.recv_ready():
            buf += ch.recv(65536)
            t0 = time.time()
            if _PROMPT.search(buf):
                time.sleep(0.06)
                if not ch.recv_ready():
                    break
            continue
        time.sleep(0.03)
        if buf and _PROMPT.search(buf):
            break
    return buf.decode("utf-8", "replace")


def _session():
    with _lock:
        if _conn["ch"] and time.time() - _conn["ts"] < 900:
            return _conn["ch"]
    t, ch = _open()
    with _lock:
        _conn.update({"t": t, "ch": ch, "ts": time.time()})
    return ch


def _drop():
    with _lock:
        try:
            if _conn["t"]:
                _conn["t"].close()
        except Exception:
            pass
        _conn.update({"t": None, "ch": None, "ts": 0})


def _num(pat, text, default=0):
    m = re.search(pat, text)
    return int(m.group(1)) if m else default


def _parse_ifaces(text):
    """按块解析 `display interface`，只保留物理口（GE / Ten-GE）"""
    blocks = []
    hits = list(_HDR.finditer(text))
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        blocks.append((m.group(1), m.group(2), text[m.start():end]))

    ports = []
    for name, state, body in blocks:
        no = int(name.rsplit("/", 1)[1])
        dm = re.search(r"Description:\s*(.*?)\s*\r?\n", body)
        desc = (dm.group(1).strip() if dm else "")
        if desc.endswith("Interface"):                  # 出厂默认描述等于没写
            desc = ""
        sm = re.search(r"(\d+)Mbps-speed mode", body)
        up = state.upper() == "UP"
        speed_mbps = int(sm.group(1)) if (sm and up) else 0
        rx = _num(r"Last 300 seconds input:\s+\d+ packets/sec\s+(\d+) bytes/sec", body)
        tx = _num(r"Last 300 seconds output:\s+\d+ packets/sec\s+(\d+) bytes/sec", body)
        err = _num(r"Input:\s+(\d+) input errors", body)
        pvid = _num(r"PVID:\s*(\d+)", body)
        lt = re.search(r"Port link-type:\s*(\w+)", body)
        link_type = lt.group(1) if lt else ""
        vlan = ""
        if link_type == "trunk":
            vm = re.search(r"Tagged\s+VLAN ID\s*:\s*([0-9,\s]+)", body)
        else:
            vm = re.search(r"Untagged VLAN ID\s*:\s*([0-9,\s]+)", body)
        if vm:
            vlan = " ".join(vm.group(1).split())

        meta = config.SWITCH_PORTS.get(no, {})
        if speed_mbps >= 1000:
            spd = "%.0fG" % (speed_mbps / 1000.0)
        elif speed_mbps:
            spd = "%dM" % speed_mbps
        else:
            spd = ""
        ports.append({
            "no": no,
            "ifname": "GE1/0/%d" % no,
            "desc": desc,
            "role": meta.get("role") or desc or ("口 %d" % no),
            "node": meta.get("node"),
            "up": up,
            "speed": spd,
            "speed_mbps": speed_mbps,
            "rx": rx,      # 收字节/秒
            "tx": tx,      # 发字节/秒
            "pvid": pvid,
            "link_type": link_type,
            "vlan": vlan,
            "errors": err,
        })
    ports.sort(key=lambda p: p["no"])
    return ports


def _version(ch):
    with _lock:
        if _ver["data"] and time.time() - _ver["ts"] < 1800:
            return _ver["data"]
    txt = _cmd(ch, "display version")
    uptime = ""
    model = ""
    m = re.search(r"^(H3C\s+\S+?)\s+uptime is\s+(.+)$", txt, re.M)
    if m:
        model, uptime = m.group(1).strip(), m.group(2).strip()
    if not model:
        m2 = re.search(r"^(H3C\s+S\S+)\s*$", txt, re.M)
        model = m2.group(1).strip() if m2 else ""
    ver = ""
    m3 = re.search(r"Comware Software, Version\s+([\d.]+,\s*Release\s*\w+)", txt)
    if m3:
        ver = "Comware " + m3.group(1)
    data = {"model": model, "version": ver, "uptime": uptime}
    with _lock:
        _ver.update({"ts": time.time(), "data": data})
    return data


def collect():
    if not config.SWITCH_URL:
        raise RuntimeError("未配置 SWITCH_URL")
    for attempt in (0, 1):
        try:
            ch = _session()
            txt = _cmd(ch, "display interface")
            if "current state" not in txt:
                raise RuntimeError("没有读到接口输出，会话可能失效")
            ports = _parse_ifaces(txt)
            if not ports:
                raise RuntimeError("接口输出解析为空")
            info = _version(ch)
            with _lock:
                _conn["ts"] = time.time()
            return {
                "ok": True,
                "model": info.get("model") or "H3C",
                "version": info.get("version") or "",
                "uptime": info.get("uptime") or "",
                "ports": ports,
                "up_count": sum(1 for p in ports if p["up"]),
                "total_ports": len(ports),
                "poe_used_w": 0.0,
                "poe_ports": 0,
                "jitter_count": 0,
                "ts": int(time.time()),
            }
        except Exception:
            _drop()
            if attempt:
                raise
    return {"ok": False, "ports": [], "error": "unreachable"}


def collect_usage():
    """保留接口：H3C 的累计包数在大屏上不用"""
    return {}

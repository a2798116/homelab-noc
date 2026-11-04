# -*- coding: utf-8 -*-
"""iKuai 爱快 4.0 API 采集器

登录: POST /Action/login  {"username","passwd":md5,"pass":base64(user)}  -> Set-Cookie sess_key
调用: POST /Action/call     {"func_name","action":"show","param":{...}}
"""
import base64
import hashlib
import http.cookiejar
import json
import ssl
import threading
import time
import urllib.error
import urllib.request

import config


class IkuaiClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._opener = None
        self._logged_at = 0.0

    def _build(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        cj = http.cookiejar.CookieJar()
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj),
            urllib.request.HTTPSHandler(context=ctx),
        )

    def _post(self, path, payload, timeout=8):
        opener = self._opener
        req = urllib.request.Request(
            config.IKUAI_URL + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def login(self):
        with self._lock:
            self._opener = self._build()
            md5 = hashlib.md5(config.IKUAI_PASS.encode("utf-8")).hexdigest()
            b64 = base64.b64encode(config.IKUAI_USER.encode("utf-8")).decode()
            res = self._post("/Action/login", {
                "username": config.IKUAI_USER, "passwd": md5,
                "pass": b64, "remember_password": "",
            })
            if res.get("code") != 0:
                raise RuntimeError("iKuai 登录失败: %s" % res.get("message"))
            self._logged_at = time.time()

    def call(self, func, param=None):
        if self._opener is None or time.time() - self._logged_at > 1500:
            self.login()
        payload = {"func_name": func, "action": "show", "param": param or {}}
        try:
            res = self._post("/Action/call", payload)
        except Exception:
            self.login()
            res = self._post("/Action/call", payload)
        if res.get("code") not in (0, None):
            self.login()
            res = self._post("/Action/call", payload)
        return res.get("results") or {}


_client = IkuaiClient()


def _pct(s):
    try:
        return float(str(s).rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def collect_system():
    """CPU / 内存 / 在线数 / 连接数 / 实时流速"""
    if not config.IKUAI_URL:
        raise RuntimeError("未配置 IKUAI_URL（见 .env.example）")
    r = _client.call("sysstat", {"TYPE": "cpu,memory,uptime,online_user,stream"})
    cpu = r.get("cpu") or []
    cpu_vals = [_pct(x) for x in cpu]
    mem = r.get("memory") or {}
    stream = r.get("stream") or {}
    online = r.get("online_user") or {}
    return {
        "cpu": round(sum(cpu_vals) / len(cpu_vals), 1) if cpu_vals else 0,
        "cpu_cores": cpu_vals,
        "mem_used_pct": _pct(mem.get("used")),
        "mem_total_kb": mem.get("total", 0),
        "mem_used_kb": mem.get("total", 0) - mem.get("available", 0),
        "uptime": r.get("uptime", 0),
        "online": online.get("count", 0),
        "online_wired": online.get("count_wired", 0),
        "online_wireless": online.get("count_wireless", 0),
        "conn_total": stream.get("connect_num", 0),
        "conn_tcp": stream.get("tcp_connect_num", 0),
        "conn_udp": stream.get("udp_connect_num", 0),
        "conn_icmp": stream.get("icmp_connect_num", 0),
        "up_bps": stream.get("upload", 0),
        "down_bps": stream.get("download", 0),
        "up_total": stream.get("total_up", 0),
        "down_total": stream.get("total_down", 0),
    }


def collect_interfaces():
    """wan1 / lan1 实时流速 + 物理网口速率链路"""
    r = _client.call("monitor_iface", {"TYPE": "iface_stream"})
    out = {}
    for row in r.get("iface_stream", []) or []:
        name = row.get("interface")
        out[name] = {
            "iface": name,
            "ip": row.get("ip_addr"),
            "up": row.get("upload", 0),          # bytes/s
            "down": row.get("download", 0),
            "up_total": row.get("total_up", 0),
            "down_total": row.get("total_down", 0),
            "drop_up": row.get("updropped", 0),
            "drop_down": row.get("downdropped", 0),
        }
    try:
        phy = _client.call("wan", {"TYPE": "ether_info"})
        out["_phy"] = [
            {"port": k, "mac": v.get("mac"), "link": v.get("link", 0),
             "speed": v.get("speed", 0), "iface": v.get("interface")}
            for k, v in (phy.get("ether_info") or {}).items()
        ]
    except Exception:
        out["_phy"] = []
    return out


def collect_wan_check():
    r = _client.call("monitor_iface", {"TYPE": "iface_check"})
    rows = r.get("iface_check") or []
    if not rows:
        return {}
    row = rows[0]
    return {
        "ip": row.get("ip_addr"),
        "gateway": row.get("gateway"),
        "internet": row.get("internet"),
        "ok": row.get("result") == "success",
        "msg": row.get("errmsg"),
    }


def collect_devices():
    """全屋终端列表"""
    r = _client.call("monitor_lanip", {
        "TYPE": "data,total", "limit": "0,300",
        "ORDER_BY": "ip_addr_int", "ORDER": "asc",
    })
    devices = []
    for d in r.get("data", []) or []:
        devices.append({
            "ip": d.get("ip_addr"),
            "mac": (d.get("mac") or "").upper(),
            "hostname": d.get("hostname") or "",
            "vendor": d.get("client_vendor") or "",
            "model": (d.get("client_model") or "").replace("%20", " "),
            "comment": (d.get("comment") or "").replace("%20", " ").replace("%28", "(").replace("%29", ")"),
            "type": d.get("client_type") or "",
            "conn": d.get("connect_num", 0),
            "up": d.get("upload", 0),
            "down": d.get("download", 0),
            "up_total": d.get("total_up", 0),
            "down_total": d.get("total_down", 0),
            "today_total": d.get("today_total", 0),
            "uptime": d.get("uptime") or "",
            "static": bool(d.get("static_status")),
            "iface": d.get("interface") or "",
        })
    return devices

# -*- coding: utf-8 -*-
"""远程爱快（VPN 站点）API 采集器

和本地 ikuai.py 的区别：
  * 连接参数走 config.RIKUAI_URL / RIKUAI_USER / RIKUAI_PASS，不读本地爱快配置；
  * 远程爱快是 V3 老版本，返回格式是 {"Result":30000,"ErrMsg":"Success","Data":{...}}，
    数据在 Data 字段里（V4 是 results）；
  * 登录 payload 用 V4 的 md5+salt_11+base64 格式也能被 V3 接受；
  * 只采算力页需要的字段（sysstat），不采接口/终端/VPN。

地址用 VPN 隧道地址（如 10.8.8.X），隧道断时连接超时，前端降级显示「离线」。
"""
import base64
import hashlib
import http.cookiejar
import json
import ssl
import threading
import time
import urllib.request

import config

_lock = threading.RLock()


class _Client:
    def __init__(self):
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
        req = urllib.request.Request(
            config.RIKUAI_URL + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with self._opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _login(self):
        self._opener = self._build()
        md5 = hashlib.md5(config.RIKUAI_PASS.encode("utf-8")).hexdigest()
        b64 = base64.b64encode(("salt_11" + md5).encode()).decode()
        res = self._post("/Action/login", {
            "username": config.RIKUAI_USER, "passwd": md5,
            "pass": b64, "remember_password": "",
        })
        # V3: {"Result":10000,"ErrMsg":"Success"}；V4: {"code":0}
        if res.get("Result") not in (10000, None) and res.get("code") not in (0, None):
            raise RuntimeError("远程爱快登录失败: %s" % res.get("ErrMsg") or res.get("message"))
        self._logged_at = time.time()

    def call(self, func, param=None):
        with _lock:
            if self._opener is None or time.time() - self._logged_at > 1500:
                self._login()
            payload = {"func_name": func, "action": "show", "param": param or {}}
            last = None
            for _ in range(3):
                try:
                    res = self._post("/Action/call", payload)
                    # V3 成功: Result in (10000, 30000)；V4 成功: code in (0, None)
                    ok_v3 = res.get("Result") in (10000, 30000, None)
                    ok_v4 = res.get("code") in (0, None)
                    if ok_v3 or ok_v4:
                        return res.get("Data") or res.get("results") or {}
                    last = res
                except Exception as exc:
                    last = exc
                try:
                    self._login()
                except Exception:
                    pass
            if isinstance(last, Exception):
                raise last
            return {}


_client = _Client()


def _pct(s):
    try:
        return float(str(s).rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def collect():
    """sysstat：CPU/内存/在线数/连接数/实时流速"""
    if not config.RIKUAI_URL:
        raise RuntimeError("未配置 RIKUAI_URL（见 .env.example）")
    r = _client.call("sysstat", {"TYPE": "cpu,memory,uptime,online_user,stream"})
    cpu = r.get("cpu") or []
    cpu_vals = [_pct(x) for x in cpu]
    mem = r.get("memory") or {}
    stream = r.get("stream") or {}
    online = r.get("online_user") or {}
    mem_total_kb = mem.get("total", 0)
    mem_avail_kb = mem.get("available", mem.get("free", 0))
    return {
        "ok": True,
        "host": config.RIKUAI_URL,
        "cpu": round(sum(cpu_vals) / len(cpu_vals), 1) if cpu_vals else 0,
        "cores": len(cpu_vals),
        "cpu_cores": [round(v, 1) for v in cpu_vals],
        "mem_used_pct": _pct(mem.get("used")),
        "mem_total": mem_total_kb * 1024,
        "mem_used": max(mem_total_kb - mem_avail_kb, 0) * 1024,
        "uptime": r.get("uptime", 0),
        "online": online.get("count", 0),
        "online_wired": online.get("count_wired", 0),
        "online_wireless": online.get("count_wireless", 0),
        "conn_total": stream.get("connect_num", 0),
        "up_bps": stream.get("upload", 0),
        "down_bps": stream.get("download", 0),
    }

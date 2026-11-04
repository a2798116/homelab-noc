# -*- coding: utf-8 -*-
"""PVE Proxmox VE API 采集器

认证: POST /api2/json/access/ticket  (username/password, urlencoded) -> data.ticket
调用: GET  /api2/json/...           Header Cookie: PVEAuthCookie=...
"""
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config


class PveClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._ticket = None
        self._expire = 0.0
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    def login(self):
        with self._lock:
            body = urllib.parse.urlencode({
                "username": config.PVE_USER, "password": config.PVE_PASS,
            }).encode()
            req = urllib.request.Request(
                config.PVE_URL + "/api2/json/access/ticket", data=body, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=10, context=self._ctx) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))["data"]
            self._ticket = data["ticket"]
            self._expire = time.time() + 3600

    def get(self, path):
        if not self._ticket or time.time() > self._expire:
            self.login()
        req = urllib.request.Request(config.PVE_URL + "/api2/json" + path)
        req.add_header("Cookie", "PVEAuthCookie=" + self._ticket)
        try:
            with urllib.request.urlopen(req, timeout=10, context=self._ctx) as r:
                return json.loads(r.read().decode("utf-8", "replace")).get("data")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self.login()
                req = urllib.request.Request(config.PVE_URL + "/api2/json" + path)
                req.add_header("Cookie", "PVEAuthCookie=" + self._ticket)
                with urllib.request.urlopen(req, timeout=10, context=self._ctx) as r:
                    return json.loads(r.read().decode("utf-8", "replace")).get("data")
            raise


_client = PveClient()
_prev_net = {}
_prev_ts = {}


def _rate(vmid, netin, netout):
    now = time.time()
    prev = _prev_net.get(vmid)
    pt = _prev_ts.get(vmid, now)
    dt = max(now - pt, 0.1)
    _prev_net[vmid] = (netin or 0, netout or 0)
    _prev_ts[vmid] = now
    if not prev:
        return 0, 0
    return max(int(((netin or 0) - prev[0]) / dt), 0), max(int(((netout or 0) - prev[1]) / dt), 0)


def collect():
    if not config.PVE_URL:
        raise RuntimeError("未配置 PVE_URL（见 .env.example）")
    node = config.PVE_NODE
    st = _client.get("/nodes/%s/status" % node) or {}
    mem = st.get("memory") or {}
    rootfs = st.get("rootfs") or {}
    swap = st.get("swap") or {}
    cpuinfo = st.get("cpuinfo") or {}

    guests = []
    for kind, path in (("qemu", "/nodes/%s/qemu" % node), ("lxc", "/nodes/%s/lxc" % node)):
        try:
            rows = _client.get(path) or []
        except Exception:
            rows = []
        for g in rows:
            vmid = g.get("vmid")
            rx, tx = _rate(vmid, g.get("netin"), g.get("netout"))
            guests.append({
                "vmid": vmid,
                "name": g.get("name") or ("VM %s" % vmid),
                "kind": kind,
                "status": g.get("status"),
                "cpu": round((g.get("cpu") or 0) * 100, 1),
                "cpus": g.get("cpus"),
                "mem": g.get("mem") or 0,
                "maxmem": g.get("maxmem") or 1,
                "maxdisk": g.get("maxdisk") or 0,
                "uptime": g.get("uptime") or 0,
                "netin": rx,
                "netout": tx,
                "netin_total": g.get("netin") or 0,
                "netout_total": g.get("netout") or 0,
            })
    guests.sort(key=lambda x: x["vmid"])

    return {
        "node": node,
        "cpu": round((st.get("cpu") or 0) * 100, 1),
        "cpus": cpuinfo.get("cpus") or 0,
        "model": cpuinfo.get("model") or "",
        "mhz": cpuinfo.get("mhz") or "",
        "mem_used": mem.get("used") or 0,
        "mem_total": mem.get("total") or 1,
        "swap_used": swap.get("used") or 0,
        "swap_total": swap.get("total") or 1,
        "disk_used": rootfs.get("used") or 0,
        "disk_total": rootfs.get("total") or 1,
        "uptime": st.get("uptime") or 0,
        "version": st.get("pveversion") or "",
        "kernel": st.get("kversion") or "",
        "guests": guests,
    }

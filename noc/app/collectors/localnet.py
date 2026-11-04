# -*- coding: utf-8 -*-
"""本机（FN-Lab）内核读数：网卡吞吐 / 负载 / 内存 / 磁盘"""
import os
import time

_prev = {}
_prev_ts = 0.0


def _read_dev():
    data = {}
    try:
        with open("/proc/net/dev", "r", encoding="utf-8", errors="replace") as f:
            for line in f.readlines()[2:]:
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                name = name.strip()
                if name == "lo":
                    continue
                parts = rest.split()
                if len(parts) < 9:
                    continue
                data[name] = {"rx": int(parts[0]), "tx": int(parts[8])}
    except OSError:
        pass
    return data


def _read_mem():
    out = {"total": 0, "available": 0}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                k, v = line.split(":", 1)
                if k == "MemTotal":
                    out["total"] = int(v.split()[0]) * 1024
                elif k == "MemAvailable":
                    out["available"] = int(v.split()[0]) * 1024
    except OSError:
        pass
    return out


def _read_disk(path="/"):
    try:
        s = os.statvfs(path)
        total = s.f_blocks * s.f_frsize
        free = s.f_bavail * s.f_frsize
        return {"total": total, "free": free, "used": total - free}
    except Exception:
        return {"total": 0, "free": 0, "used": 0}


def collect():
    global _prev, _prev_ts
    now = time.time()
    dt = max(now - _prev_ts, 0.1) if _prev_ts else 0
    cur = _read_dev()
    nics = []
    total_rx = total_tx = 0
    for name, v in cur.items():
        rx = tx = 0
        if dt and name in _prev:
            rx = max(int((v["rx"] - _prev[name]["rx"]) / dt), 0)
            tx = max(int((v["tx"] - _prev[name]["tx"]) / dt), 0)
        total_rx += rx
        total_tx += tx
        nics.append({"name": name, "rx": rx, "tx": tx})

    _prev = cur
    _prev_ts = now

    mem = _read_mem()
    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError):
        # Windows 没有 getloadavg，本地调试时降级为 0
        load1 = load5 = load15 = 0.0

    disk = _read_disk("/")
    vol = _read_disk("/vol1") if os.path.isdir("/vol1") else None
    store = vol if (vol and vol["total"]) else disk

    return {
        "nics": sorted(nics, key=lambda x: -(x["rx"] + x["tx"])),
        "rx": total_rx,
        "tx": total_tx,
        "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "cores": os.cpu_count() or 1,
        "mem_used": mem["total"] - mem["available"],
        "mem_total": mem["total"] or 1,
        "disk_used": store["used"],
        "disk_total": store["total"] or 1,
        "vol_used": store["used"],
        "vol_total": store["total"] or 1,
        "vol_mount": "/vol1" if (vol and vol["total"]) else "/",
        "uptime": _uptime(),
    }


def _uptime():
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            return int(float(f.read().split()[0]))
    except OSError:
        return 0

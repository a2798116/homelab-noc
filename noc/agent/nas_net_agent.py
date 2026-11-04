#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极空间 NAS 网络与负载采集端

部署形态：极空间 Docker 容器，network_mode=host，读宿主机 /proc，
每 N 秒向 NOC 上报一次。零第三方依赖。

关键取舍：只取 bond0。极空间把 eth0/eth1 做成 active-backup bond，
bond0 计数 = eth0 + eth1（实测完全相等）；而 ifb0 是流量整形的镜像设备，
docker0/br-*/veth* 是容器内部流量，计入会重复。
"""
import json
import os
import time
import urllib.error
import urllib.request

NOC = os.environ.get("NOC_URL", "").rstrip("/")
TOKEN = os.environ.get("NOC_TOKEN", "")
INTERVAL = max(int(os.environ.get("INTERVAL", "5")), 2)
PRIMARY = os.environ.get("PRIMARY_IFACE", "bond0")
NAME = os.environ.get("NAS_NAME", "NAS")


def _die_missing():
    if not NOC or not TOKEN:
        raise SystemExit("必须设置 NOC_URL 与 NOC_TOKEN 环境变量（模板见 compose-create.example.json）")


def read_dev():
    out = {}
    try:
        with open("/proc/net/dev", "r") as fh:
            lines = fh.readlines()[2:]
    except OSError:
        return out
    for line in lines:
        name, _, rest = line.partition(":")
        name = name.strip()
        fields = rest.split()
        if not name or len(fields) < 9:
            continue
        try:
            out[name] = (int(fields[0]), int(fields[8]))
        except ValueError:
            continue
    return out


def read_mem():
    total = avail = 0
    try:
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                key, _, val = line.partition(":")
                if key == "MemTotal":
                    total = int(val.split()[0])
                elif key == "MemAvailable":
                    avail = int(val.split()[0])
    except (OSError, ValueError, IndexError):
        return None
    if not total:
        return None
    return {"total_kb": total,
            "used_pct": round((total - avail) / float(total) * 100.0, 1)}


def read_load():
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        return None


def read_temp():
    """/sys/class/thermal 里挑一个可读的 CPU 温度。"""
    base = "/sys/class/thermal"
    try:
        for entry in sorted(os.listdir(base)):
            if not entry.startswith("thermal_zone"):
                continue
            try:
                with open(os.path.join(base, entry, "type")) as fh:
                    kind = fh.read().strip().lower()
                if "cpu" not in kind and "soc" not in kind:
                    continue
                with open(os.path.join(base, entry, "temp")) as fh:
                    raw = int(fh.read().strip())
                return round(raw / 1000.0, 1) if raw > 1000 else round(raw, 1)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return None


def post(payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        NOC + "/api/nas-report", data=body,
        headers={"Content-Type": "application/json", "X-Noc-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=8) as resp:
        resp.read()
        return resp.status


def main():
    _die_missing()
    prev, prev_t = None, 0.0
    print("[agent] -> %s  primary=%s  every %ss" % (NOC, PRIMARY, INTERVAL),
          flush=True)
    while True:
        started = time.time()
        cur = read_dev()
        payload = {"name": NAME, "ts": int(started), "iface": PRIMARY,
                   "mem": read_mem(), "load": read_load(), "temp": read_temp()}

        if PRIMARY in cur:
            rx_total, tx_total = cur[PRIMARY]
            payload["rx_total"] = rx_total
            payload["tx_total"] = tx_total
            if prev and PRIMARY in prev and prev_t:
                dt = max(started - prev_t, 0.5)
                drx = rx_total - prev[PRIMARY][0]
                dtx = tx_total - prev[PRIMARY][1]
                # 计数器重置/回绕时差值为负，按 0 处理
                payload["rx_bps"] = max(int(drx / dt), 0) if drx >= 0 else 0
                payload["tx_bps"] = max(int(dtx / dt), 0) if dtx >= 0 else 0
        else:
            payload["error"] = "iface %s not found" % PRIMARY

        try:
            post(payload)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            print("[agent] 上报失败: %s" % exc, flush=True)

        prev, prev_t = cur, started
        time.sleep(max(INTERVAL - (time.time() - started), 0.5))


if __name__ == "__main__":
    main()

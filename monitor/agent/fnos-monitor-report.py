#!/usr/bin/env python3
"""Homelab Monitor 采集端

在每台 fnOS / Linux 设备上运行，读取传感器与系统状态，
POST 到中心看板。仅用标准库，适配系统自带的 python3。

配置：/etc/fnos-monitor.env
    SERVER=http://<中心地址>:8930
    TOKEN=<设备令牌>
    DEVICE_NAME=FN7567          # 可选，首次上报时作为展示名

用法：
    fnos-monitor-report.py            # 采集并上报
    fnos-monitor-report.py --dry-run  # 只打印 JSON，不发送
"""
import base64
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

AGENT_VERSION = "fnos-monitor-report/1.1"
CONF_PATH = os.environ.get("MONITOR_CONF", "/etc/fnos-monitor.env")
# 需要统计使用率的挂载点，可用 MOUNTS 覆盖（PVE 等非 fnOS 设备按需配置）
DEFAULT_MOUNTS = "/,/vol1,/vol2"


def load_conf():
    conf = {}
    if os.path.isfile(CONF_PATH):
        with open(CONF_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip().strip("'\"")
    # 环境变量优先
    for k in ("SERVER", "TOKEN", "DEVICE_NAME", "DEVICE_MODEL", "AUTH_USER", "AUTH_PASS", "MOUNTS"):
        if os.environ.get(k):
            conf[k] = os.environ[k]
    return conf


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def read_int(path):
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


# ---------------- 温度 ----------------
def collect_temps():
    """按 hwmon 名称归类，返回 (标准字段, 全部原始读数)"""
    raw = {}
    cores = []
    nvme = []
    out = {}

    for zone in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = read_text(os.path.join(zone, "name")).strip()
        for tfile in sorted(glob.glob(os.path.join(zone, "temp*_input"))):
            val = read_int(tfile)
            if val is None:
                continue
            celsius = round(val / 1000.0, 1)
            raw.setdefault(name or os.path.basename(zone), []).append(celsius)

            low = name.lower()
            if "coretemp" in low or low.startswith("k10temp"):
                cores.append(celsius)
            elif "nvme" in low:
                nvme.append(celsius)
            elif "pch" in low:
                out["pch"] = max(out.get("pch") or 0, celsius) if "pch" in out else celsius
            elif "iwlwifi" in low or "wifi" in low:
                out["wifi"] = celsius
            elif "dell_smm" in low or low.startswith(("it8", "nct", "f718", "w83", "asus", "giga")):
                if "board" not in out:
                    out["board"] = celsius
            elif "acpitz" in low or low.startswith("acpi"):
                out["acpi"] = celsius

    if cores:
        out["cpu"] = max(cores)
        out["cpu_cores"] = cores
    if nvme:
        out["nvme"] = max(nvme)
        out["nvme_all"] = nvme
    out["raw"] = raw
    return out


# ---------------- CPU ----------------
def cpu_times():
    fields = read_text("/proc/stat").splitlines()[0].split()[1:]
    vals = [int(x) for x in fields]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    return total, idle


def collect_cpu():
    t1, i1 = cpu_times()
    time.sleep(1.0)
    t2, i2 = cpu_times()
    dt, di = t2 - t1, i2 - i1
    usage = round(100.0 * (dt - di) / dt, 1) if dt > 0 else 0.0

    load = read_text("/proc/loadavg").split()
    model = "unknown"
    cores = os.cpu_count() or 0
    for line in read_text("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break

    return {
        "usage": usage,
        "load1": float(load[0]) if len(load) > 0 else None,
        "load5": float(load[1]) if len(load) > 1 else None,
        "load15": float(load[2]) if len(load) > 2 else None,
        "cores": cores,
        "model": model,
    }


# ---------------- 内存 / 磁盘 / 基础信息 ----------------
def collect_mem():
    info = {}
    for line in read_text("/proc/meminfo").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            parts = v.split()
            if parts:
                try:
                    info[k.strip()] = int(parts[0]) * 1024
                except ValueError:
                    pass
    total = info.get("MemTotal")
    avail = info.get("MemAvailable")
    used = (total - avail) if (total and avail) else None
    return {
        "total": total,
        "used": used,
        "swap_total": info.get("SwapTotal"),
        "swap_used": (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) or 0,
    }


def collect_disks(conf):
    out = []
    mounts = [m.strip() for m in conf.get("MOUNTS", DEFAULT_MOUNTS).split(",") if m.strip()]
    for mount in mounts:
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        pct = round(100.0 * used / total) if total else 0
        if total <= 0:
            continue
        out.append({"mount": mount, "total": total, "used": used, "pct": pct})
    out.extend(collect_lvmthin())
    return out


def collect_lvmthin():
    """LVM thin pool 使用率（PVE 的 local-lvm 不挂载，靠 lvs 读数）

    只取 thin pool 本身，跳过各虚拟磁盘的 thin 卷（它们的 data_percent 没参考意义）。
    """
    if not shutil.which("lvs"):
        return []
    try:
        res = subprocess.run(
            ["lvs", "--noheadings", "--nosuffix", "--units", "b",
             "-o", "lv_name,vg_name,lv_size,lv_attr,data_percent"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        name, vg, size, attr, dpercent = parts[0], parts[1], parts[2], parts[3], parts[4]
        # lv_attr 首字符 t = thin pool；V = thin 卷（各虚拟磁盘），跳过
        if not attr.startswith("t") or dpercent in ("", "-"):
            continue
        try:
            total = int(float(size))
            pct = int(round(float(dpercent)))
        except ValueError:
            continue
        if total <= 0:
            continue
        out.append({"mount": "%s/%s" % (vg, name), "total": total,
                    "used": int(total * pct / 100.0), "pct": pct})
    return out


def collect_pve():
    """Proxmox VE 宿主机：汇总虚拟机/容器状态（非 PVE 返回 None）"""
    if not shutil.which("pvesh"):
        return None
    try:
        res = subprocess.run(
            ["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
            capture_output=True, text=True, timeout=15,
        )
        items = json.loads(res.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(items, list):
        return None
    vms = [x for x in items if x.get("type") == "qemu"]
    cts = [x for x in items if x.get("type") == "lxc"]
    running = [x for x in items if x.get("status") == "running"]

    def mem_of(x):
        return int(x.get("maxmem") or 0)

    guests = []
    for x in sorted(items, key=lambda y: int(y.get("vmid") or 0)):
        guests.append({
            "vmid": x.get("vmid"),
            "name": (x.get("name") or "")[:40],
            "type": "vm" if x.get("type") == "qemu" else "ct",
            "status": x.get("status"),
            "mem": mem_of(x),
            "maxcpu": x.get("maxcpu"),
            "uptime": int(x.get("uptime") or 0),
        })
    return {
        "guests_total": len(items),
        "guests_running": len(running),
        "vm_total": len(vms),
        "vm_running": len([x for x in vms if x.get("status") == "running"]),
        "ct_total": len(cts),
        "ct_running": len([x for x in cts if x.get("status") == "running"]),
        "mem_alloc": sum(mem_of(x) for x in running),
        "vcpu_alloc": sum(int(x.get("maxcpu") or 0) for x in running),
        "guests": guests[:40],
    }


def collect_meta(conf):
    kernel = read_text("/proc/version").split(" ")[2] if read_text("/proc/version") else ""
    meta = {
        "hostname": socket.gethostname(),
        "name": conf.get("DEVICE_NAME") or socket.gethostname(),
        "kernel": kernel,
        "model": conf.get("DEVICE_MODEL", ""),
        "agent": AGENT_VERSION,
    }
    if shutil.which("pveversion"):
        try:
            res = subprocess.run(["pveversion"], capture_output=True, text=True, timeout=8)
            line = (res.stdout or "").splitlines()[0]
            if line:
                meta["pve"] = line.replace("pve-manager/", "").split("/")[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    return meta


def main():
    dry = "--dry-run" in sys.argv
    conf = load_conf()
    server = conf.get("SERVER", "").rstrip("/")
    token = conf.get("TOKEN", "")

    if not server or not token:
        print("配置缺失：请在 %s 中设置 SERVER 和 TOKEN" % CONF_PATH, file=sys.stderr)
        return 2

    payload = {
        "ts": int(time.time()),
        "temps": collect_temps(),
        "cpu": collect_cpu(),
        "mem": collect_mem(),
        "uptime": int(float(read_text("/proc/uptime").split()[0] or 0)),
        "disks": collect_disks(conf),
        "meta": collect_meta(conf),
    }
    pve = collect_pve()
    if pve:
        payload["pve"] = pve
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    if dry:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    headers = {"Content-Type": "application/json", "X-Device-Token": token}
    auth_user = conf.get("AUTH_USER")
    if auth_user:
        raw = "%s:%s" % (auth_user, conf.get("AUTH_PASS", ""))
        headers["Authorization"] = "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")

    req = urllib.request.Request(
        server + "/api/report",
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            print("[ok] %s %s" % (resp.status, text[:200]))
    except urllib.error.HTTPError as exc:
        print("[fail] HTTP %s: %s" % (exc.code, exc.read().decode("utf-8", errors="replace")[:300]), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print("[fail] %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

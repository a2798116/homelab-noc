# -*- coding: utf-8 -*-
"""远程飞牛 NAS (fnOS) SSH 采集器

经 VPN 可达的远程 fnOS 主机，SSH 进去读 /proc/stat / meminfo / df /
thermal / uptime，算出 CPU / 内存 / 磁盘 / 温度 / 运行时长。

和 switch.py 的区别：fnOS 是标准 Linux，SSH 算法现代，不需要塞回老 kex。
连接超时设短（8s），VPN 断时快速失败，前端降级显示「离线」。

CPU 占用率要两次采样求差：第一轮没有 prev，返回 0；第二轮起才有真实值。
和 pve.py 的 _rate 是同一套思路。
"""
import re
import threading
import time

import config

try:
    import paramiko
except ImportError:
    paramiko = None

_lock = threading.Lock()
_prev_cpu = {"total": 0, "idle": 0, "ts": 0.0}

# 一次 SSH 会话把所有指标拉回来，减少连接开销
_CMD = (
    "echo '===CPU==='; head -1 /proc/stat; "
    "echo '===MEM==='; grep -E '^(MemTotal|MemAvailable|MemFree|Buffers|Cached):' /proc/meminfo; "
    "echo '===DF==='; df -B1 --output=size,used,avail,target 2>/dev/null; "
    "echo '===THERMAL==='; cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null; "
    "echo '===UPTIME==='; cat /proc/uptime; "
    "echo '===LOAD==='; cat /proc/loadavg; "
    "echo '===MODEL==='; grep -m1 'model name' /proc/cpuinfo; "
    "echo '===CORES==='; grep -c '^processor' /proc/cpuinfo; "
    "echo '===KERNEL==='; uname -r; "
    "echo '===HOST==='; hostname"
)


def _connect():
    if paramiko is None:
        raise RuntimeError("未安装 paramiko（容器镜像需包含该依赖）")
    if not config.FNOS_HOST:
        raise RuntimeError("未配置 FNOS_HOST（见 .env.example）")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(
        config.FNOS_HOST, port=config.FNOS_PORT,
        username=config.FNOS_USER, password=config.FNOS_PASS,
        timeout=8, banner_timeout=8, auth_timeout=8,
        allow_agent=False, look_for_keys=False,
    )
    return cli


def _run(cli, cmd):
    _, stdout, _ = cli.exec_command(cmd, timeout=12)
    return stdout.read().decode("utf-8", "replace")


def _disk_label(mount):
    """把飞牛的存储池路径翻成短标签，前端不用截断"""
    if mount == "/":
        return "根分区"
    m = re.match(r"^/(vol\d+)$", mount)
    if m:
        return m.group(1)
    # /vol02/1000-4-2e2b9c5e → vol02/1000-4（截掉哈希尾）
    m = re.match(r"^/(vol\d+)/(1000-\d+)", mount)
    if m:
        return m.group(1) + "/" + m.group(2)
    return mount


def _parse(out):
    """把一坨带分隔符的输出拆成结构化 dict"""
    secs = {}
    cur = None
    for line in out.splitlines():
        tag = line.strip()
        if tag.startswith("===") and tag.endswith("==="):
            cur = tag[3:-3].strip()
            secs[cur] = []
        elif cur is not None:
            secs[cur].append(line)

    # ---- CPU 占用率（两次采样求差）----
    cpu = 0.0
    m = re.match(r"cpu\s+(\d+(?:\s+\d+)+)", (secs.get("CPU") or [""])[0])
    if m:
        vals = [int(x) for x in m.group(1).split()]
        total = sum(vals)
        idle = vals[3] + vals[4] if len(vals) > 4 else vals[3]
        now = time.time()
        with _lock:
            pt, pt_total, pt_idle = _prev_cpu["ts"], _prev_cpu["total"], _prev_cpu["idle"]
            _prev_cpu.update(total=total, idle=idle, ts=now)
        if pt and total > pt_total:
            dt, di = total - pt_total, idle - pt_idle
            cpu = max(round((dt - di) / dt * 100, 1), 0.0)

    # ---- 内存 ----
    mem = {}
    for line in (secs.get("MEM") or []):
        mm = re.match(r"(MemTotal|MemAvailable|MemFree|Buffers|Cached):\s+(\d+)", line)
        if mm:
            mem[mm.group(1)] = int(mm.group(2)) * 1024
    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable")
    if mem_avail is None:
        # 老内核没有 MemAvailable，退回 free+buffers+cached
        mem_avail = mem.get("MemFree", 0) + mem.get("Buffers", 0) + mem.get("Cached", 0)
    mem_used = max(mem_total - mem_avail, 0)

    # ---- 磁盘：挑根分区和数据卷（飞牛 /vol1 等）----
    disks = []
    for line in (secs.get("DF") or []):
        parts = line.split()
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        size, used, avail, target = parts[0], parts[1], parts[2], parts[3]
        # 只留根分区和 /vol 开头的数据卷，忽略 tmpfs / dev / run / docker overlay
        if target == "/" or (target.startswith("/vol") and "/docker/" not in target and "/overlay" not in target) or target == "/home":
            disks.append({
                "mount": target,
                "label": _disk_label(target),
                "total": int(size),
                "used": int(used),
                "avail": int(avail),
            })

    # ---- 温度：取所有 thermal_zone 的最大值（毫摄氏度 → 摄氏度）----
    temps = []
    for line in (secs.get("THERMAL") or []):
        s = line.strip()
        if s.isdigit():
            temps.append(round(int(s) / 1000.0, 1))
    temp = max(temps) if temps else None

    # ---- 运行时长 / 负载 ----
    uptime = 0
    up_line = (secs.get("UPTIME") or [""])[0]
    m = re.match(r"(\d+(?:\.\d+)?)", up_line)
    if m:
        uptime = int(float(m.group(1)))
    load = (secs.get("LOAD") or [""])[0].split()
    load1 = float(load[0]) if load and load[0] else 0.0

    # ---- 静态信息 ----
    model = ""
    mm = re.match(r"model name\s*:\s*(.+)", (secs.get("MODEL") or [""])[0])
    if mm:
        model = mm.group(1).strip()
    cores = 0
    cs = (secs.get("CORES") or [""])[0].strip()
    if cs.isdigit():
        cores = int(cs)
    kernel = (secs.get("KERNEL") or [""])[0].strip()
    hostname = (secs.get("HOST") or [""])[0].strip()

    return {
        "ok": True,
        "host": config.FNOS_HOST,
        "hostname": hostname,
        "cpu": cpu,
        "cores": cores,
        "model": model,
        "kernel": kernel,
        "mem_used": mem_used,
        "mem_total": mem_total,
        "disks": disks,
        "temp": temp,
        "uptime": uptime,
        "load1": load1,
    }


def collect():
    """SSH 进远程飞牛采一轮。VPN 断或密码错时抛异常，由 server.py 记 error。"""
    cli = _connect()
    try:
        out = _run(cli, _CMD)
    finally:
        try:
            cli.close()
        except Exception:
            pass
    return _parse(out)

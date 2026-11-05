# -*- coding: utf-8 -*-
"""局域网全量存活探测：并发 ping 终端墙上每一台设备

iKuai 的 monitor_lanip 只给"网关自己看见过"的终端：IoT 设备长时间不上报、
同网段互访为主的机器，都会被周期性从在线表里踢掉。踢掉之后我们没有别的
证据，只能把格子变灰——可设备明明还活着。

这里用最简单粗暴的办法补证据：在容器里对墙上所有设备各发一个 ICMP，1 秒超时、
32 路并发，几十台设备 2~3 秒跑完。结果并入 probes 状态，_probe_up() 就能据此
把误判离线的设备捞回来。

探测范围只覆盖"已经出现在墙上或资产表里的 IP"，不扫整个网段——扫描会把
访客手机、临时设备全炸出来，那不是这张大屏想看的。
"""
import concurrent.futures as cf
import subprocess

import config

# 单次 ping 超时（秒）。局域网内 1 秒足够，超时太长会拖住整轮。
_TIMEOUT = 1
_PARALLEL = 32


def _ping(ip):
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(_TIMEOUT), "-q", ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_TIMEOUT + 1.5,
        )
        return ip, r.returncode == 0
    except Exception:
        return ip, False


def _same_site(ip):
    """只探与 LAN_PREFIX 前两段一致的私有地址，避免误探异站点与公网"""
    base = ".".join(config.LAN_PREFIX.strip(".").split(".")[:2]) + "."
    return ip.startswith(base)


def collect(ips):
    """返回 {ip: True/False}，只含本次真正探过的地址"""
    targets = []
    seen = set()
    for ip in ips or []:
        if not ip or ip in seen or not _same_site(ip):
            continue
        seen.add(ip)
        targets.append(ip)
    if not targets:
        return {}

    out = {}
    with cf.ThreadPoolExecutor(max_workers=_PARALLEL) as ex:
        for ip, up in ex.map(_ping, targets):
            out[ip] = up
    return out

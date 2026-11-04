# -*- coding: utf-8 -*-
"""TCP 连接探测：用 connect 耗时近似 RTT，附带抖动与丢包

不依赖 root，不依赖 ping 二进制，纯 socket。
"""
import socket
import statistics
import time

import config

_history = {}


def _tcp_rtt(ip, port, timeout=1.2):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.perf_counter()
    try:
        s.connect((ip, port))
        return (time.perf_counter() - t0) * 1000.0
    except OSError:
        return None
    finally:
        s.close()


def collect():
    out = []
    for t in config.PROBE_TARGETS:
        key = t["key"]
        samples = []
        fails = 0
        for _ in range(3):
            rtt = _tcp_rtt(t["ip"], t["port"])
            if rtt is None:
                fails += 1
            else:
                samples.append(rtt)
        hist = _history.setdefault(key, [])
        if samples:
            avg = statistics.mean(samples)
            hist.append(avg)
            del hist[:-20]
            jitter = 0.0
            if len(hist) >= 3:
                jitter = statistics.pstdev(hist[-8:])
            out.append({
                "key": key, "name": t["name"], "ip": t["ip"], "port": t["port"],
                "rtt": round(avg, 2), "jitter": round(jitter, 2),
                "loss": round(fails / 3 * 100, 1),
                "up": True,
            })
        else:
            hist.clear()
            out.append({
                "key": key, "name": t["name"], "ip": t["ip"], "port": t["port"],
                "rtt": None, "jitter": 0, "loss": 100.0, "up": False,
            })
    return out

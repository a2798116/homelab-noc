# -*- coding: utf-8 -*-
"""无线 AP 采集：SSID / 在线数 / 客户端归属 —— 数据源为爱快 AC

爱快免费版禁用了 AP 详情接口（monitor_ap / ap_getinfo 返回 Access denied），
但终端表（monitor_lanip）里每个无线客户端都带 apname（归属 AP）、ssid、
frequencies、signal 字段。本采集器据此推算：

- 每台 AP 的在线终端数 = apname 匹配的终端条数
- 每台 AP 的 SSID = 其终端里出现过的 ssid 去重
- 客户端明细 = 终端行的 mac / ip / 型号 / 实时速率 / 信号

AP 清单在 config.local.json 的 wifi.hosts 里声明，host 字段填爱快里的
AP 名称（如 IK-W7_286d），label/model/node 用于界面显示与拓扑对位。
"""
import threading
import time

import config

try:
    from collectors import ikuai
except ImportError:  # 直接以模块方式调试时
    import ikuai

_lock = threading.Lock()
_cache = {"ts": 0.0, "rows": []}
CACHE_TTL = 8  # 秒：AP 采集 10s 一次，终端表 5s 一轮，允许复用


def _terminals():
    """带短缓存的终端原始行（复用 iKuai 客户端会话）"""
    with _lock:
        if _cache["rows"] and time.time() - _cache["ts"] < CACHE_TTL:
            return _cache["rows"]
    r = ikuai._client.call("monitor_lanip", {
        "TYPE": "data,total", "limit": "0,300",
        "ORDER_BY": "ip_addr_int", "ORDER": "asc",
    })
    rows = r.get("data", []) or []
    with _lock:
        _cache["ts"] = time.time()
        _cache["rows"] = rows
    return rows


def _rate(v):
    """'26M' / '1.2M' / 0 → 字节数（爱快 uprate/downrate 是带单位的字符串）"""
    try:
        s = str(v or "").strip().upper().rstrip("BMPS")
        return int(float(s) * 1024 * 1024) if s else 0
    except (TypeError, ValueError):
        return 0


def collect():
    if not config.MIWIFI_HOSTS:
        raise RuntimeError("未配置无线 AP（config.local.json 的 wifi.hosts）")
    try:
        rows = _terminals()
    except Exception as e:
        raise RuntimeError("爱快终端表不可用: %s" % str(e)[:120])

    # apname → 终端行分组（apname 可能带机型后缀，用前缀匹配配置里的名称）
    aps = []
    clients = []
    seen = set()
    for meta in config.MIWIFI_HOSTS:
        ap = {
            "host": meta.get("host") or "",
            "label": meta.get("label") or meta.get("host") or "",
            "model": meta.get("model") or "iKuai AP",
            "ip": meta.get("ip") or "",
            "zone": meta.get("zone") or "",
            "node": meta.get("node"),
            "ok": False,
            "ssid": "", "channels": [], "online": 0, "clients": [],
            "up": 0, "down": 0,
        }
        key = (meta.get("host") or "").lower()
        if key:
            matched = [d for d in rows
                       if (d.get("apname") or "").lower().startswith(key)]
            ap["online"] = len(matched)
            ssids = []
            for d in matched:
                mac = (d.get("mac") or "").upper()
                ssid = d.get("ssid") or ""
                if ssid and ssid not in ssids:
                    ssids.append(ssid)
                if not mac or mac in seen:
                    continue
                seen.add(mac)
                ap["clients"].append({
                    "mac": mac,
                    "ip": d.get("ip_addr") or "",
                    "name": (d.get("client_model") or d.get("hostname") or
                             d.get("termname") or "").replace("%20", " "),
                    "dev": "2.4G" if d.get("frequencies") == "2g" else "5G",
                    # upload/download 才是实时流量；uprate/downrate 是 802.11 协商速率，
                    # 用它们当吞吐会算出上百 MB/s 的假数据，因此单独放在 phy_* 字段
                    "up": d.get("upload") or 0,
                    "down": d.get("download") or 0,
                    "phy_up": _rate(d.get("uprate")),
                    "phy_down": _rate(d.get("downrate")),
                    "online_sec": 0,
                    "signal": d.get("signal"),
                })
            ap["ssid"] = " / ".join(ssids)
            ap["up"] = sum(c["up"] for c in ap["clients"])
            ap["down"] = sum(c["down"] for c in ap["clients"])
            ap["ok"] = True
        aps.append(ap)

    total_online = sum(a["online"] for a in aps)
    for a in aps:
        for c in a["clients"]:
            c = dict(c)
            c["ap"] = a["label"]
            clients.append(c)
    return {
        "ok": any(a["ok"] for a in aps),
        "aps": aps,
        "online": total_online,
        "clients": clients,
        "ts": int(time.time()),
    }

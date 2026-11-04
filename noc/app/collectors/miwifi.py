# -*- coding: utf-8 -*-
"""小米路由器（AP 模式）采集：SSID / 信道 / 在线数 / 拓扑 / 无线客户端

两台 AP 均为有线中继，不负责拨号，因此 wan 字段恒为 0，只取无线侧数据。
"""
import json
import html
import threading
import time
import urllib.parse
import urllib.request

import config

_lock = threading.Lock()
_tokens = {}      # host -> {"token": str, "ts": float}


def _post(host, path, payload):
    url = "http://%s%s" % (host, path)
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get(host, token, path):
    url = "http://%s/cgi-bin/luci/;stok=%s%s" % (host, token, path)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _token(host, force=False):
    with _lock:
        c = _tokens.get(host)
        if c and not force and time.time() - c["ts"] < 900:
            return c["token"]
    res = _post(host, "/cgi-bin/luci/api/xqsystem/login", {
        "username": config.MIWIFI_USER,
        "password": config.MIWIFI_PASS,
        "logtype": "1",
    })
    tok = res.get("token")
    if not tok:
        raise RuntimeError("小米 AP 登录失败: %s" % str(res)[:100])
    with _lock:
        _tokens[host] = {"token": tok, "ts": time.time()}
    return tok


def _call(host, path):
    for attempt in (0, 1):
        tok = _token(host, force=bool(attempt))
        try:
            return _get(host, tok, path)
        except Exception:
            if attempt:
                raise
            with _lock:
                _tokens.pop(host, None)
    return {}


def _band(ifname):
    return "5G" if ifname in ("wl0", "wifi1") else "2.4G"


def collect_ap(meta):
    host = meta["host"]
    out = {
        "host": host, "label": meta["label"], "model": meta["model"],
        "node": meta["node"], "ok": False,
        "ssid": "", "channels": [], "online": 0, "clients": [],
    }
    try:
        topo = _call(host, "/api/misystem/topo_graph")
        g = topo.get("graph") or {}
        out["ssid"] = html.unescape(g.get("ssid") or "")
        out["locale"] = g.get("locale") or ""
        out["online"] = int(g.get("onlines") or 0)
        # 主 AP 的拓扑里带下挂 AP 的在线数，叶子节点自己查不到
        out["leafs"] = [
            {"ip": lf.get("ip"), "onlines": int(lf.get("onlines") or 0),
             "locale": lf.get("locale") or "", "name": lf.get("name") or "",
             "link": lf.get("link_type") or ""}
            for lf in (g.get("leafs") or [])
        ]

        wifi = _call(host, "/api/xqnetwork/wifi_detail_all")
        seen = set()
        for it in wifi.get("info") or []:
            if it.get("mode") != "Master":
                continue
            band = _band(it.get("ifname", ""))
            if band in seen:
                continue
            seen.add(band)
            ci = it.get("channelInfo") or {}
            # channel 字段是配置值（0=自动），真实信道在 channelInfo 里
            ch = ci.get("channel") or it.get("channel") or 0
            out["channels"].append({
                "band": band,
                "ch": int(ch) if str(ch).isdigit() else 0,
                "width": int(ci.get("bandwidth") or 0),
                "mode": it.get("wifimode") or "",
                "txpwr": it.get("txpwr") or "",
                "ssid": html.unescape(it.get("ssid") or ""),
            })

        dev = _call(host, "/api/xqsystem/device_list")
        for d in dev.get("list") or []:
            st = d.get("statistics") or {}
            mac = (d.get("mac") or "").upper()
            if not mac:
                continue
            # 子 AP 只返回 MAC 清单（无 IP/无速率），仍保留用于归属判定
            out["clients"].append({
                "mac": mac,
                "ip": d.get("ip") or "",
                "name": d.get("name") or d.get("hostname") or "",
                "dev": st.get("dev") or "",
                "up": float(st.get("upspeed") or 0) * 1024,     # KB/s → B/s
                "down": float(st.get("downspeed") or 0) * 1024,
                "online_sec": int(float(st.get("online") or 0)),
            })

        stat = _call(host, "/api/misystem/status")
        mem = stat.get("mem") or {}
        if mem.get("total"):
            out["mem_total"] = mem.get("total")
            out["mem_pct"] = round(float(mem.get("usage") or 0) * 100, 1)
        hw = stat.get("hardware") or {}
        out["firmware"] = hw.get("version") or ""
        out["display"] = hw.get("displayName") or ""
        cpu = stat.get("cpu") or {}
        out["cpu_core"] = cpu.get("core")
        out["uptime"] = int(float(stat.get("upTime") or 0))

        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)[:160]
    return out


def collect():
    if not config.MIWIFI_HOSTS:
        raise RuntimeError("未配置小米 AP（config.local.json 的 wifi.hosts）")
    aps = [collect_ap(m) for m in config.MIWIFI_HOSTS]

    # 子 AP 自己报不出在线数，从主 AP 拓扑的 leafs 反查
    by_ip = {a["host"]: a for a in aps}
    for a in aps:
        for lf in a.get("leafs") or []:
            tgt = by_ip.get(lf.get("ip"))
            if tgt is not None and tgt is not a:
                tgt["online"] = lf["onlines"]
                tgt["locale"] = lf.get("locale") or tgt.get("locale")
                tgt["uplink"] = lf.get("link") or ""

    total_online = sum(a.get("online", 0) for a in aps if a.get("ok"))
    clients = []
    seen = set()
    for a in aps:
        for c in a.get("clients") or []:
            if c["mac"] in seen:
                continue
            seen.add(c["mac"])
            c["ap"] = a["label"]
            clients.append(c)
    return {
        "ok": any(a.get("ok") for a in aps),
        "aps": aps,
        "online": total_online,
        "clients": clients,
        "ts": int(time.time()),
    }

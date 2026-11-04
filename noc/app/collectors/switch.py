# -*- coding: utf-8 -*-
"""水星 SE106P 交换机采集：端口 link 状态 / 实时收发速率 / PoE 功率

登录用 TP-Link 系的 securityEncode（源码里叫 hex_md5，实为异或映射）。
速率单位：设备返回 kbps，这里统一换算成 bytes/s 供前端使用。
"""
import http.cookiejar
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config

_KEY = "RDpbLfCPsJZ7fiv"
_TBL = (
    "yLwVl0zKqws7LgKPRQ84Mdt708T1qQ3Ha7xv3H7NyU84p21BriUWBU43odz3iP4rBL3cD02KZciX"
    "TysVXiV8ngg6vL48rPJyAUw0HurW20xqxv9aYb4M9wK1Ae0wlro510qXeU07kV57fQMc8L6aLgML"
    "wygtc0F10a0Dg70TOoouyFhdysuRMO51yY5ZlOZZLEal1h0t9YQW0Ko7oBwmCAHoic4HYbUyVeU3"
    "sfQ1xtXcPcf1aT303wAQhv66qzW"
)
# spd_act 编码 → 速率文本
_SPEED = {0: "断开", 1: "自动", 2: "10M", 3: "10M", 4: "100M", 5: "100M",
          6: "1G", 7: "2.5G", 8: "10G"}
_SPEED_MBPS = {0: 0, 1: 0, 2: 10, 3: 10, 4: 100, 5: 100, 6: 1000, 7: 2500, 8: 10000}

_lock = threading.Lock()
_sess = {"op": None, "ts": 0}
_login_fail = {"count": 0, "ts": 0}
# 口 → 上次确切的速率，用于把偶发的 spd_act=0 补回来
_last_speed = {}


def _encode(pwd):
    """TP-Link securityEncode"""
    e, c, j = len(pwd), len(_KEY), len(_TBL)
    out = []
    for g in range(max(e, c)):
        l = i = 187
        if g >= e:
            i = ord(_KEY[g])
        elif g >= c:
            l = ord(pwd[g])
        else:
            l = ord(pwd[g])
            i = ord(_KEY[g])
        out.append(_TBL[(l ^ i) % j])
    return "".join(out)


def _new_session():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    return op


def _fetch(op, path, data=None):
    url = config.SWITCH_URL + path
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    req.add_header("Referer", config.SWITCH_URL + "/Menu.htm")
    if body:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with op.open(req, timeout=8) as r:
        return r.read().decode("utf-8", "replace")


def _login():
    op = _new_session()
    _fetch(op, "/")
    body = _fetch(op, "/logon.cgi", {
        "username": config.SWITCH_USER,
        "password": _encode(config.SWITCH_PASS),
        "logon": "Login",
    })
    m = re.search(r"logonInfo\s*=\s*new Array\(\s*(\d+)", body)
    code = int(m.group(1)) if m else -1
    if code != 0:
        # 实测：0=成功；非 0 为失败
        raise RuntimeError("交换机登录失败 logonInfo=%s" % code)
    return op


def _session():
    with _lock:
        if _sess["op"] and time.time() - _sess["ts"] < 600:
            return _sess["op"]
    op = _login()
    with _lock:
        _sess["op"] = op
        _sess["ts"] = time.time()
    return op


def _drop_session():
    with _lock:
        _sess["op"] = None
        _sess["ts"] = 0


def _arr(text, name):
    m = re.search(r"%s\s*:\s*\[([0-9,\s\.]*)\]" % re.escape(name), text)
    if not m:
        return []
    vals = []
    for v in m.group(1).split(","):
        v = v.strip()
        if not v:
            continue
        try:
            vals.append(float(v))
        except ValueError:
            vals.append(0.0)
    return vals


def _arr_str(text, name):
    m = re.search(r"%s\s*:\s*\[([^\[\]]*)\]" % re.escape(name), text)
    if not m:
        return []
    return [x.strip() for x in m.group(1).split(",")]


def _parse_main(html):
    rx = _arr(html, "rx_rate")      # kbps
    tx = _arr(html, "tx_rate")
    spd = _arr(html, "spd_act")
    poe_port = _arr(html, "is_poe_port")
    poe_power = _arr(html, "power")
    state = _arr(html, "state")

    ports = []
    total = max(len(rx), len(tx), len(spd))
    for i in range(total):
        no = i + 1
        meta = config.SWITCH_PORTS.get(no, {"role": "端口 %d" % no, "node": None})
        spd_code = int(spd[i]) if i < len(spd) else 0
        rx_kbps = rx[i] if i < len(rx) else 0
        tx_kbps = tx[i] if i < len(tx) else 0
        st_on = bool(state[i]) if i < len(state) else False
        # 关键：spd_act 偶发读成 0，但同一时刻 rx/tx 仍有值。
        # 端口真的 down 时收发速率必然是 0，所以"有流量"足以证明链路是通的。
        has_traffic = (rx_kbps + tx_kbps) > 0
        ports.append({
            "no": no,
            "role": meta["role"],
            "node": meta["node"],
            "spd_code": spd_code,
            "up": spd_code > 0 or has_traffic,
            "speed": _SPEED.get(spd_code, "?"),
            "speed_mbps": _SPEED_MBPS.get(spd_code, 0),
            "rx_kbps": rx_kbps,
            "tx_kbps": tx_kbps,
            # kbps → bytes/s
            "rx": rx_kbps * 125,
            "tx": tx_kbps * 125,
            "poe_capable": bool(poe_port[i]) if i < len(poe_port) else False,
            "poe_w": (poe_power[i] / 10.0) if i < len(poe_power) else 0.0,
            "enabled": st_on,
            # 速率读数不可信，已用历史值补全
            "jitter": bool(spd_code == 0 and has_traffic),
        })
    return ports


def _settle(ports):
    """把速率读数抖动压掉：重取一次，仍不确定就沿用上次已知速率。

    水星 SE106P 的 MainRpm 在 2.5G 口上偶发把 spd_act 写成 0（实测约 1/6 的采样），
    同时 rx/tx 却是正常值——端口并没有断。这里只修显示，不动 up 判定。
    """
    shaky = [p for p in ports if p["jitter"]]
    if shaky:
        try:
            html2 = _fetch(_session(), "/MainRpm.htm")
            again = {p["no"]: p for p in _parse_main(html2)}
            for p in shaky:
                q = again.get(p["no"])
                if q and q["spd_code"] > 0:
                    p.update({
                        "spd_code": q["spd_code"],
                        "speed": q["speed"],
                        "speed_mbps": q["speed_mbps"],
                        "jitter": False,
                    })
        except Exception:
            pass

    for p in ports:
        if p["spd_code"] > 0:
            _last_speed[p["no"]] = (p["speed"], p["speed_mbps"])
        elif p["jitter"] and p["no"] in _last_speed:
            sp, mb = _last_speed[p["no"]]
            p["speed"] = sp + "?"      # 问号 = 沿用上次，本轮没读到
            p["speed_mbps"] = mb
    return ports


def collect():
    if not config.SWITCH_URL:
        raise RuntimeError("未配置 SWITCH_URL（见 .env.example）")
    for attempt in (0, 1):
        try:
            op = _session()
            html = _fetch(op, "/MainRpm.htm")
            if "rx_rate" not in html:
                raise RuntimeError("页面无速率字段，会话可能失效")
            ports = _settle(_parse_main(html))
            up_count = sum(1 for p in ports if p["up"])
            return {
                "ok": True,
                "ports": ports,
                "up_count": up_count,
                "total_ports": len(ports),
                "poe_used_w": round(sum(p["poe_w"] for p in ports), 1),
                "poe_ports": sum(1 for p in ports if p["poe_capable"]),
                "jitter_count": sum(1 for p in ports if p["jitter"]),
                "ts": int(time.time()),
            }
        except Exception:
            _drop_session()
            if attempt:
                raise
    return {"ok": False, "ports": [], "error": "unreachable"}


def collect_usage():
    """端口统计页：累计收发包数（用于计算 PPS）"""
    try:
        op = _session()
        html = _fetch(op, "/PortStatisticsRpm.htm")
        pkts = _arr(html, "pkts")
        return {"pkts": pkts}
    except Exception:
        _drop_session()
        return {}

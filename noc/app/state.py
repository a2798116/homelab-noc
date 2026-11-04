# -*- coding: utf-8 -*-
"""全局状态聚合：快照 / 历史曲线 / 事件流 / 健康评分"""
import threading
import time
from collections import deque

import assets
import config

_lock = threading.Lock()
_state = {
    "started": time.time(),
    "ts": 0,
    "ikuai": None,
    "wan": None,
    "lan": None,
    "ifaces": {},
    "pve": None,
    "local": None,
    "switch": None,
    "miwifi": None,
    "ha": None,
    "nas": None,
    "probes": [],
    "devices": [],
    "errors": {},
}

_history = deque(maxlen=config.HISTORY_POINTS)
_events = deque(maxlen=300)
_seen_ips = set()
_prev_dev_speed = {}
# 终端上下线降噪：最后在线时间 / 上次播报时间 / 首次消失时间 / 播过离线的 IP
_dev_last_seen = {}
_dev_event_ts = {}
_dev_gone = {}
_dev_offline = set()
# 端口号 → {last, cand, n, flips, warned}，链路事件去抖用
_port_watch = {}
_DEBOUNCE_N = 2
_prev_ha_avail = {}
# 灯 / 窗帘上一次状态，用来产生"客厅吸顶灯 开"这类事件
_prev_lamp = {}
_prev_cover = {}
# MAC(大写) → {"ap": 名称, "node": 拓扑节点, "band": 频段}
_wifi_map = {}


def _classify(dev):
    ip = dev.get("ip", "")
    meta = assets.DEVICE_META.get(ip)
    if meta:
        out = dict(meta)
        out["known"] = True
    else:
        cat = "client"
        blob = "%s %s %s" % (dev.get("vendor", ""), dev.get("model", ""), dev.get("hostname", ""))
        for rule in config.CATEGORY_RULES:
            if any(k.lower() in blob.lower() for k in rule["match"]):
                cat = rule["cat"]
                break
        out = {
            "name": dev.get("model") or dev.get("hostname") or dev.get("vendor") or "未知设备",
            "zone": "未知", "icon": _guess_icon(dev), "cat": cat,
            "hw": dev.get("vendor") or "", "note": "", "known": False,
        }
    dev.update({
        "display": out["name"],
        "zone": out["zone"],
        "icon": out["icon"],
        "cat": out["cat"],
        "hw": out.get("hw", ""),
        "note": out.get("note", ""),
        "known": out.get("known", False),
    })
    dev["cat_label"] = assets.CATEGORY_LABEL.get(dev["cat"], "其他")
    return dev


def _guess_icon(dev):
    blob = ("%s %s %s" % (dev.get("vendor", ""), dev.get("model", ""), dev.get("hostname", ""))).lower()
    table = [
        ("router", ["router", "路由"]), ("ap", ["ap", "mesh"]),
        ("camera", ["camera", "摄像头", "ipc"]), ("speaker", ["soundbox", "音箱"]),
        ("phone", ["iphone", "android", "手机"]), ("laptop", ["windows", "笔记本"]),
        ("printer", ["epson", "printer", "打印"]), ("nas", ["zspace", "nas"]),
        ("switch", ["switch", "交换"]), ("server", ["linux", "proxmox", "server"]),
    ]
    for icon, keys in table:
        if any(k in blob for k in keys):
            return icon
    return "device"


def _add_event(kind, level, text, detail=""):
    _events.appendleft({
        "t": int(time.time()), "kind": kind, "level": level,
        "text": text, "detail": detail,
    })


def update(section, value):
    with _lock:
        _state[section] = value
        _state["ts"] = int(time.time())
        _state["errors"].pop(section, None)


def set_error(section, err):
    with _lock:
        _state["errors"][section] = str(err)[:200]


def _probe_up(ip):
    """NOC 自己的 TCP 探测说通，就不采信 iKuai 的离线判定。

    iKuai 的终端表只统计"经过网关或它 ARP 探测到的"设备。PVE 宿主机、NAS、交换机
    这类同网段互访为主的机器，长期不跟网关说话就会被 iKuai 周期性踢出在线表——
    实测 10.10 在表外时 TCP 8006 一直通，宿主机 uptime 连续没断。
    """
    for p in _state.get("probes") or []:
        if p.get("ip") == ip and p.get("up"):
            return True
    return False


def update_devices(rows):
    """终端列表：补全静态资产、检测上下线、写入快照

    三层降噪：
    1. 能自证活着的（probes 探通）不判离线，只标 ikuai_seen=False
    2. 剩下的给 DEV_GRACE 秒宽限期，期内继续显示但标 stale
    3. 同一设备 DEV_REPEAT 秒内不重复播报上下线
    """
    now = time.time()
    with _lock:
        prev = {d["ip"]: d for d in _state["devices"]}
        out = []
        for d in rows:
            rnd = d.get("mac", "")
            if rnd.startswith(("32:", "5a:", "56:", "12:", "ca:", "da:", "02:")) and len(rnd) == 17:
                d["random_mac"] = True
            d["last_seen"] = int(now)
            w = _wifi_map.get((d.get("mac") or "").upper())
            if w:
                d["wifi_ap"] = w["ap"]
                d["wifi_band"] = w["band"]
                d["wifi_node"] = w["node"]
            d["stale"] = False
            d["ikuai_seen"] = True
            out.append(_classify(d))

        cur = {d["ip"]: d for d in out}
        for ip in cur:
            _dev_last_seen[ip] = now

        # ---- 本轮到 iKuai 列表里没了的设备 ----
        for ip, old in prev.items():
            if ip in cur:
                # 只有真播过"离线"的设备回来才播"上线"。iKuai 漏报一轮又报回来，
                # 设备其实一直在，播出去只会制造噪声。
                if ip in _dev_offline:
                    if now - _dev_event_ts.get(ip, 0) >= config.DEV_REPEAT:
                        _add_event("online", "info", "%s 上线" % cur[ip].get("display", ip), ip)
                        _dev_event_ts[ip] = now
                    _dev_offline.discard(ip)
                _dev_gone.pop(ip, None)
                continue

            if _probe_up(ip):
                # 探测可达：只是 iKuai 没看见，继续当在线
                keep = dict(old)
                keep["ikuai_seen"] = False
                keep["stale"] = False
                out.append(keep)
                cur[ip] = keep
                _dev_gone.pop(ip, None)
                continue

            first = _dev_gone.setdefault(ip, now)
            if now - first < config.DEV_GRACE:
                # 宽限期内先灰着，别让墙上的格子闪
                keep = dict(old)
                keep["stale"] = True
                out.append(keep)
                cur[ip] = keep
                continue

            if now - _dev_event_ts.get(ip, 0) >= config.DEV_REPEAT:
                _add_event("offline", "warn", "%s 离线" % old.get("display", ip), ip)
                _dev_event_ts[ip] = now
                _dev_offline.add(ip)
            _dev_last_seen.pop(ip, None)
            _dev_gone.pop(ip, None)

        # ---- 探活可达、但 iKuai 完全不报的设备（交换机/AP 这类不跟网关说话的）----
        # 不补的话它们永远进不了终端墙：实测 10.3 交换机在 iKuai 表里 25/25 次缺席
        for pr in _state.get("probes") or []:
            ip = pr.get("ip") or ""
            if ip in cur or not pr.get("up"):
                continue
            if not ip.startswith(config.LAN_PREFIX) or ip not in assets.DEVICE_META:
                continue
            old = prev.get(ip)
            if old:
                keep = dict(old)
            else:
                keep = _classify({
                    "ip": ip, "mac": "", "hostname": "", "vendor": "",
                    "model": "", "comment": "", "type": "", "conn": 0,
                    "up": 0, "down": 0, "up_total": 0, "down_total": 0,
                    "today_total": 0, "uptime": "", "static": False, "iface": "",
                })
                keep["last_seen"] = int(now)
            keep["ikuai_seen"] = False
            keep["stale"] = False
            out.append(keep)
            cur[ip] = keep

        # ---- 新出现的设备（上一轮完全没有）----
        for ip, d in cur.items():
            if ip in prev or d.get("stale") or not prev:
                continue
            if d.get("ikuai_seen") is False:
                continue          # 探活补进来的，iKuai 从没确认过，不播报
            if now - _dev_event_ts.get(ip, 0) >= config.DEV_REPEAT:
                _add_event("online", "info", "%s 上线" % d.get("display", ip), ip)
                _dev_event_ts[ip] = now
            _dev_offline.discard(ip)

        _state["devices"] = out


def update_switch(payload):
    """交换机端口：记录状态变化事件

    必须去抖。水星 SE106P 的 spd_act 在 2.5G 口上会偶发读成 0（实测约 1/6 采样），
    单次读数直接判 down 会让事件流被"口5 断开/恢复"刷屏，而链路一直是通的。
    """
    ports = payload.get("ports") or []
    now = time.time()
    with _lock:
        for p in ports:
            no = p["no"]
            cur = bool(p["up"])
            w = _port_watch.get(no)
            if w is None:
                # 首次观测只登记，不发事件
                _port_watch[no] = {"last": cur, "cand": None, "n": 0,
                                   "flips": deque(maxlen=32), "warned": 0}
                continue
            if cur == w["last"]:
                w["cand"] = None
                w["n"] = 0
                continue
            # 状态变了先记候选，连续 DEBOUNCE_N 次都变才认
            if w["cand"] == cur:
                w["n"] += 1
            else:
                w["cand"] = cur
                w["n"] = 1
            if w["n"] < _DEBOUNCE_N:
                continue

            if cur:
                _add_event("link", "info", "交换机 口%d 链路恢复（%s）" % (no, p["speed"]), p["role"])
            else:
                _add_event("link", "warn", "交换机 口%d 链路断开" % no, p["role"])
            w["last"] = cur
            w["cand"] = None
            w["n"] = 0
            w["flips"].append(now)

            # 真反复翻动（不是读数抖动）才提示查线
            recent = [t for t in w["flips"] if now - t < 300]
            if len(recent) >= 6 and not w["warned"]:
                w["warned"] = now
                _add_event("link", "crit", "交换机 口%d 链路反复中断" % no,
                           "5 分钟内 %d 次，检查网线、水晶头与端口协商" % len(recent))
            elif w["warned"] and now - w["warned"] > 900:
                w["warned"] = 0
        _state["switch"] = payload
        _state["ts"] = int(time.time())
        _state["errors"].pop("switch", None)


def update_wifi(payload):
    """小米 AP：维护 MAC → 归属 AP 映射，供终端详情显示接入点"""
    m = {}
    for a in payload.get("aps") or []:
        for c in a.get("clients") or []:
            mac = (c.get("mac") or "").upper()
            if not mac:
                continue
            dev = c.get("dev") or ""
            band = "5G" if dev.startswith("wl0") else ("2.4G" if dev.startswith("wl1") else "")
            m[mac] = {"ap": a.get("label") or a.get("host"), "node": a.get("node"),
                      "band": band, "up": c.get("up"), "down": c.get("down")}
    with _lock:
        _wifi_map.clear()
        _wifi_map.update(m)
        _state["miwifi"] = payload
        _state["ts"] = int(time.time())
        _state["errors"].pop("miwifi", None)


def update_ha(payload):
    """Home Assistant：实体骤降告警 + 灯/窗帘开关进事件流"""
    if not payload.get("ok"):
        return
    avail = payload.get("available") or 0
    total = payload.get("total") or 0
    with _lock:
        prev = _prev_ha_avail.get("ratio")
        if total:
            ratio = avail / total
            if prev is not None and prev - ratio > 0.15:
                _add_event("ha", "warn", "HA 实体大量掉线",
                           "%d → %d（共 %d）" % (round(prev * total), avail, total))
            _prev_ha_avail["ratio"] = ratio

        # 灯与窗帘的开关 —— 事件流里就看得见"谁开了哪盏灯"
        lamps = {l["id"]: l for l in (payload.get("lamps") or [])}
        for lid, l in lamps.items():
            was = _prev_lamp.get(lid)
            if was is not None and was != l["on"]:
                _add_event("ha", "info",
                           "%s%s" % (l["area"] + " " if l.get("area") else "",
                                     l["name"]) + (" 开" if l["on"] else " 关"))
        _prev_lamp.clear()
        _prev_lamp.update({k: v["on"] for k, v in lamps.items()})

        covers = {c["name"] + "|" + c["area"]: c for c in (payload.get("covers") or [])}
        for ck, c in covers.items():
            was = _prev_cover.get(ck)
            if was is not None and was != c["open"]:
                _add_event("ha", "info", "%s %s" % (c["name"], "打开" if c["open"] else "关闭"))
        _prev_cover.clear()
        _prev_cover.update({k: v["open"] for k, v in covers.items()})

        _state["ha"] = payload
        _state["ts"] = int(time.time())
        _state["errors"].pop("ha", None)


def update_nas_report(payload):
    """极空间采集端上报：bond0 物理吞吐 + 负载。

    大二层里「手机 → NAS」的流量在交换芯片内部转发，iKuai 不计数，
    交换机也看不到同口内部流量。只有 NAS 自己的网卡计数知道真相，
    所以这一项必须由 NAS 上的容器推过来。
    """
    prev = _state.get("nas") or {}
    with _lock:
        prev_ts = prev.get("ts") or 0
        gap = time.time() - prev_ts
        _state["nas"] = {
            "name": payload.get("name") or "极空间",
            "iface": payload.get("iface") or "",
            "ts": payload.get("ts") or int(time.time()),
            "rx_bps": payload.get("rx_bps") or 0,
            "tx_bps": payload.get("tx_bps") or 0,
            "rx_total": payload.get("rx_total") or 0,
            "tx_total": payload.get("tx_total") or 0,
            "mem": payload.get("mem"),
            "load": payload.get("load"),
            "temp": payload.get("temp"),
            "error": payload.get("error") or "",
        }
        _state["ts"] = int(time.time())
        _state["errors"].pop("nas", None)
        # 从掉线状态恢复（间隔超过 staleness 阈值）时记一条
        if prev_ts and gap > config.NAS_STALE:
            _add_event("nas", "info", "极空间采集端恢复上报",
                       "中断 %.0f 秒" % gap)


def nas_view(snap_devices):
    """把 NAS 上报与 iKuai 侧外网流量做差，得出二层内网流量。"""
    nas = _state.get("nas")
    if not nas:
        return None
    age = max(time.time() - (nas.get("ts") or 0), 0)
    out = dict(nas)
    out["age"] = int(age)
    out["ok"] = age <= config.NAS_STALE and not nas.get("error")

    total_rx = nas.get("rx_bps") or 0
    total_tx = nas.get("tx_bps") or 0
    ext = next((d for d in (snap_devices or []) if d.get("ip") == config.NAS_IP), {})
    ext_rx = ext.get("down") or 0      # iKuai 看到的 .30 下载 = NAS 收
    ext_tx = ext.get("up") or 0        # iKuai 看到的 .30 上传 = NAS 发

    out.update({
        "total_rx": total_rx,
        "total_tx": total_tx,
        "total_bps": total_rx + total_tx,
        "ext_rx": ext_rx,
        "ext_tx": ext_tx,
        "ext_bps": ext_rx + ext_tx,
        # 只留正差，避免采样抖动出现负数
        "l2_rx": max(total_rx - ext_rx, 0),
        "l2_tx": max(total_tx - ext_tx, 0),
    })
    out["l2_bps"] = out["l2_rx"] + out["l2_tx"]
    return out


def snapshot_history():
    with _lock:
        ikuai = _state.get("ikuai") or {}
        wan = _state.get("wan") or {}
        lan = _state.get("lan") or {}
        pve = _state.get("pve") or {}
        local = _state.get("local") or {}
        sw = _state.get("switch") or {}
        ports = {p["no"]: p for p in (sw.get("ports") or [])}
        row = {
            "t": int(time.time()),
            "wan_up": wan.get("up", 0),
            "wan_down": wan.get("down", 0),
            "lan_up": lan.get("up", 0),
            "lan_down": lan.get("down", 0),
            "cpu": ikuai.get("cpu", 0),
            "mem": ikuai.get("mem_used_pct", 0),
            "conn": ikuai.get("conn_total", 0),
            "online": ikuai.get("online", 0),
            "pve_cpu": pve.get("cpu", 0),
            "pve_mem": round(pve.get("mem_used", 0) / max(pve.get("mem_total", 1), 1) * 100, 1),
            "lab_rx": local.get("rx", 0),
            "lab_tx": local.get("tx", 0),
        }
        nas = _state.get("nas") or {}
        row["nas_rx"] = nas.get("rx_bps", 0)
        row["nas_tx"] = nas.get("tx_bps", 0)
        for no in (1, 2, 4, 5):
            p = ports.get(no) or {}
            row["p%d_rx" % no] = p.get("rx", 0)
            row["p%d_tx" % no] = p.get("tx", 0)
        _history.append(row)
        if len(_history) > 2:
            _check_anomaly()


def _check_anomaly():
    if len(_history) < 30:
        return
    now = _history[-1]
    prev = _history[-30]
    if now["wan_up"] + now["wan_down"] > 60e6 and (prev["wan_up"] + prev["wan_down"]) < 8e6:
        _add_event("spike", "warn", "外网流量突增",
                   "%.1f MB/s" % ((now["wan_up"] + now["wan_down"]) / 1e6))


def history(minutes=30):
    n = int(minutes * 60 / max(config.POLL_FAST, 0.5))
    with _lock:
        rows = list(_history)[-n:]
    return rows


def events(limit=40):
    with _lock:
        return list(_events)[:limit]


def health():
    """健康评分：正常安静，异常尖叫"""
    with _lock:
        s = dict(_state)
    score = 100
    issues = []

    wan = s.get("wan") or {}
    if wan and not wan.get("ok", True):
        score -= 45
        issues.append({"level": "crit", "text": "WAN 拨号异常"})

    for p in s.get("probes") or []:
        if p["key"] == "inet":
            continue
        if not p["up"]:
            score -= 12
            issues.append({"level": "crit", "text": "%s 探测失败" % p["name"]})

    ikuai = s.get("ikuai") or {}
    if ikuai.get("cpu", 0) > 90:
        score -= 8
        issues.append({"level": "warn", "text": "路由 CPU %.0f%%" % ikuai["cpu"]})
    if ikuai.get("mem_used_pct", 0) > 92:
        score -= 10
        issues.append({"level": "warn", "text": "路由内存 %.0f%%" % ikuai["mem_used_pct"]})

    pve = s.get("pve") or {}
    if pve.get("mem_total"):
        pct = pve["mem_used"] / pve["mem_total"] * 100
        if pct > 92:
            score -= 10
            issues.append({"level": "warn", "text": "PVE 内存 %.0f%%" % pct})
    if pve.get("disk_total"):
        pct = pve["disk_used"] / pve["disk_total"] * 100
        if pct > 90:
            score -= 8
            issues.append({"level": "warn", "text": "PVE 系统盘 %.0f%%" % pct})

    local = s.get("local") or {}
    if local.get("vol_total"):
        pct = local["vol_used"] / local["vol_total"] * 100
        if pct > 88:
            score -= 6
            issues.append({"level": "warn", "text": "存储池 %.0f%%" % pct})

    # 交换机：挂了设备的口必须是 up
    sw = s.get("switch") or {}
    if sw.get("ok") is False:
        score -= 10
        issues.append({"level": "warn", "text": "交换机不可达"})
    for p in sw.get("ports") or []:
        if p.get("node") and not p.get("up"):
            score -= 9
            issues.append({"level": "crit", "text": "交换机 口%d 断开（%s）" % (p["no"], p["role"])})

    # 无线：AP 掉线
    wifi = s.get("miwifi") or {}
    if wifi and wifi.get("ok") is False:
        score -= 10
        issues.append({"level": "warn", "text": "小米 AP 不可达"})
    for a in wifi.get("aps") or []:
        if not a.get("ok"):
            score -= 8
            issues.append({"level": "crit", "text": "%s 离线" % a.get("label")})

    # Home Assistant：实体大面积不可用说明中枢或集成出问题
    ha = s.get("ha") or {}
    if ha and not ha.get("ok"):
        score -= 6
        issues.append({"level": "warn", "text": "HA 采集失败"})
    elif ha.get("total"):
        ratio = ha.get("available", 0) / ha["total"]
        if ratio < 0.35:
            score -= 8
            issues.append({"level": "warn", "text": "HA 仅 %.0f%% 实体可用" % (ratio * 100)})

    # 水浸/漏水：这是真正要尖叫的
    for w in ha.get("leaks") or []:
        if w.get("wet"):
            score -= 30
            issues.append({"level": "crit", "text": "%s 检测到漏水" % w.get("name")})

    # 极空间采集端：掉线说明二层流量盲区重新变大，不是致命但要知道
    nas = s.get("nas") or {}
    if nas and nas.get("name"):
        age = time.time() - (nas.get("ts") or 0)
        if age > config.NAS_STALE:
            score -= 5
            issues.append({"level": "warn", "text": "极空间采集端失联 %.0f 分" % (age / 60)})

    return {
        "score": max(score, 0),
        "level": "ok" if score >= 90 else ("warn" if score >= 70 else "crit"),
        "issues": issues,
    }


def full_snapshot():
    with _lock:
        s = dict(_state)
        s["devices"] = list(s.get("devices") or [])
        s["probes"] = list(s.get("probes") or [])
        errs = dict(s.get("errors") or {})
    s["health"] = health()
    s["nas"] = nas_view(s["devices"])
    s["events"] = events(30)
    s["errors"] = errs
    s["now"] = int(time.time())
    s["uptime_service"] = int(time.time() - s["started"])
    online = sum(1 for d in s["devices"] if not d.get("stale"))
    wifi = s.get("miwifi") or {}
    ha = s.get("ha") or {}
    s["summary"] = {
        "online": online,
        "stale": len(s["devices"]) - online,
        "offline": max(len(assets.DEVICE_META) - online, 0),
        "known": len(assets.DEVICE_META),
        "wifi_online": wifi.get("online", 0),
        "ap_count": sum(1 for a in (wifi.get("aps") or []) if a.get("ok")),
        "lights_on": ha.get("lights_on", 0),
        "lights_total": ha.get("lights_total", 0),
        "covers_open": ha.get("covers_open", 0),
        "covers_total": len(ha.get("covers") or []),
        "openings_on": ha.get("openings_on", 0),
        "has_door_sensor": ha.get("has_door_sensor", False),
        "presence_on": ha.get("presence_on", 0),
        "presence_total": len(ha.get("presence") or []),
        "ac_on": sum(1 for c in (ha.get("climates") or []) if not c.get("off")),
        "nas_l2": (s.get("nas") or {}).get("l2_bps", 0),
        "nas_ok": (s.get("nas") or {}).get("ok", False),
        "by_cat": _by_cat(s["devices"]),
    }
    # 站点信息交给前端：拓扑节点文字、NAS 地址都来自本地配置，不写死在前端代码里
    s["site"] = {
        "name": config.SITE_NAME,
        "lan_cidr": config.LAN_CIDR,
        "nas_ip": config.NAS_IP,
        "topology": config.TOPOLOGY_LABELS,
    }
    return s


def _by_cat(devs):
    out = {c: 0 for c in assets.CATEGORY_ORDER}
    for d in devs:
        out[d["cat"]] = out.get(d["cat"], 0) + 1
    return out

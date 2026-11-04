# -*- coding: utf-8 -*-
"""Home Assistant 采集：环境温湿度 / 空调 / 灯光明细 / 窗帘 / 门窗 / 人体存在 / 在家状态

认证走 IndieAuth flow：login_flow → code → access_token(+refresh_token)。
access_token 30 分钟过期，用 refresh_token 静默续期；refresh_token 落盘复用。
注意：/auth/token 必须用 form 编码，JSON body 会被读成空导致 unsupported_grant_type。

实体区域（房间）从 /api/template 取，10 分钟缓存一次 —— 区域改名是低频操作。
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config

_lock = threading.Lock()
_cache = {"access": None, "refresh": None, "exp": 0}
_state_file = config.HA_TOKEN_FILE
_client_id = config.HA_URL.rstrip("/") + "/"

# 区域缓存：entity_id → 房间名
_areas = {}
_areas_ts = 0.0
AREAS_TTL = 600

# 设备自带指示灯（网关/路由/摄像头的灯环），不是照明，混进来会让"灯光 6/31"虚高
_IGNORE_LIGHT_SUFFIX = ("_indicator_light",)

# 灯控藏在 switch 域的集成：领普智能开关面板、小米中控屏虚拟键
# 依据：面板按键实体形如 switch.linp_cn_xxx_pxdbw3_on_p_2_1 →「走廊开关  走廊灯 左键」
_LIGHT_SWITCH_PREFIX = ("switch.example_panel_",)
_SWITCH_KEY_RE = re.compile(r"_on_p_\d+_\d+$")

# 按键名尾巴，去掉后才是这路灯真正的名字
_KEY_SUFFIX = ("左键", "中键", "右键", "开关", "按键")
# 这些词单独留下来没有信息量，说明该用面板名
_GENERIC_NAME = ("开关", "按键", "灯", "左键", "中键", "右键")
# 环境温度里排除水箱/水温这类非室温传感器
_ENV_SKIP = ("鱼缸", "水温", "热水", "冰箱", "冰柜", "CPU", "电池")


# ---------------- token 持久化 ----------------
def _load_tokens():
    if not _state_file or not os.path.isfile(_state_file):
        return
    try:
        with open(_state_file, encoding="utf-8") as f:
            d = json.load(f)
        _cache["refresh"] = d.get("refresh_token")
        _cache["access"] = d.get("access_token")
        _cache["exp"] = float(d.get("expires_at") or 0)
    except Exception:
        pass


def _save_tokens():
    if not _state_file:
        return
    try:
        os.makedirs(os.path.dirname(_state_file) or ".", exist_ok=True)
        with open(_state_file, "w", encoding="utf-8") as f:
            json.dump({
                "access_token": _cache["access"],
                "refresh_token": _cache["refresh"],
                "expires_at": _cache["exp"],
            }, f)
    except OSError:
        pass


# ---------------- HTTP ----------------
def _post_json(path, payload, token=None, timeout=15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        config.HA_URL + path, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def _post_form(path, payload):
    req = urllib.request.Request(
        config.HA_URL + path, data=urllib.parse.urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def _get(path, token):
    req = urllib.request.Request(config.HA_URL + path,
                                 headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


# ---------------- 登录 / 续期 ----------------
def _password_login():
    if not config.HA_USER or not config.HA_PASS:
        raise RuntimeError("未配置 HA_USER / HA_PASS")
    st, flow = _post_json("/auth/login_flow", {
        "client_id": _client_id,
        "handler": ["homeassistant", None],   # 注意：第二个元素必须是 null，写字符串会 500
        "redirect_uri": _client_id,
    })
    if st != 200:
        raise RuntimeError("login_flow 失败 %s %s" % (st, flow))
    fid = flow["flow_id"]
    st, res = _post_json("/auth/login_flow/" + fid, {
        "username": config.HA_USER, "password": config.HA_PASS, "client_id": _client_id})
    if st != 200:
        raise RuntimeError("提交账号失败 %s %s" % (st, res))
    if not isinstance(res, dict) or "result" not in res:
        raise RuntimeError("HA 认证被拒: %s" % str(res)[:120])
    st, tok = _post_form("/auth/token", {
        "grant_type": "authorization_code", "code": res["result"], "client_id": _client_id})
    if st != 200:
        raise RuntimeError("取 token 失败 %s %s" % (st, tok))
    return tok


def _refresh(tok):
    st, res = _post_form("/auth/token", {
        "grant_type": "refresh_token",
        "refresh_token": tok,
        "client_id": _client_id,
    })
    if st != 200:
        raise RuntimeError("续期失败 %s %s" % (st, res))
    return res


def _token():
    with _lock:
        if _cache["access"] and time.time() < _cache["exp"] - 90:
            return _cache["access"]
    tok = None
    if _cache["refresh"]:
        try:
            tok = _refresh(_cache["refresh"])
        except Exception:
            _cache["refresh"] = None
    if not tok:
        tok = _password_login()
    with _lock:
        _cache["access"] = tok["access_token"]
        _cache["refresh"] = tok.get("refresh_token") or _cache["refresh"]
        _cache["exp"] = time.time() + float(tok.get("expires_in") or 1800)
    _save_tokens()
    return _cache["access"]


# ---------------- 区域（房间）映射 ----------------
_AREA_TPL = (
    "{% for s in states %}"
    "{% if s.domain in ['light','switch','cover','climate','binary_sensor','sensor'] %}"
    "{{ s.entity_id }}|{{ area_name(s.entity_id) }}\n"
    "{% endif %}{% endfor %}"
)


def _post_template(template, token, timeout=25):
    """注意：/api/template 返回纯文本，不是 JSON，不能用 _post_json"""
    req = urllib.request.Request(
        config.HA_URL + "/api/template",
        data=json.dumps({"template": template}).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]


def _load_areas(token):
    """entity_id → 房间名。失败就沿用旧缓存，不让区域拖垮整个采集。"""
    global _areas_ts
    if _areas and time.time() - _areas_ts < AREAS_TTL:
        return _areas
    st, body = _post_template(_AREA_TPL, token)
    if st != 200 or not body:
        return _areas
    out = {}
    for line in body.splitlines():
        eid, _, area = line.partition("|")
        eid, area = eid.strip(), area.strip()
        if eid and area:
            out[eid] = area
    if out:
        _areas.clear()
        _areas.update(out)
        _areas_ts = time.time()
    return _areas


# ---------------- 解析 ----------------
def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _split_friendly(raw):
    """HA 的 friendly_name 形如「设备名  实体名」，两空格分隔"""
    parts = [p.strip() for p in (raw or "").split("  ") if p.strip()]
    return parts or [""]


def _friendly(st, limit=14):
    """只取设备名并收短"""
    raw = (st.get("attributes") or {}).get("friendly_name")
    if not raw:
        raw = st["entity_id"].split(".")[-1].replace("_", " ")
    nm = _split_friendly(raw)[0]
    if len(nm) > limit:
        nm = nm[:limit - 1] + "…"
    return nm


def _short(nm, limit):
    return nm if len(nm) <= limit else nm[:limit - 1] + "…"


def _lamp_name(raw, eid):
    """灯具名。开关面板的键名（「走廊开关  走廊灯 左键」）比面板名更有用。"""
    parts = _split_friendly(raw)
    if len(parts) >= 2:
        # "走廊灯 左键" → ["走廊灯"]；"开关 开关" → [] → 退回面板名
        words = [w for w in parts[1].split() if w not in _KEY_SUFFIX]
        tail = "".join(words).strip()
        if len(tail) >= 2 and tail not in _GENERIC_NAME:
            return _short(tail, 16)
    return _short(parts[0] or eid, 16)


def _zone(eid, raw, area_map):
    """房间：优先 HA 区域，缺失时退回设备名前缀"""
    a = area_map.get(eid)
    if a:
        return a
    head = _split_friendly(raw)[0]
    for kw in ("客厅", "餐厅", "厨房", "主卧", "南卧", "北书房", "书房", "电梯间",
               "客卫", "主卫", "走廊", "阳台", "卫生间", "门口"):
        if kw in head:
            return kw
    return "其他"


# 开关面板名里就写着房间，比 HA 区域更可信（面板常被随手挂到客厅）
_PANEL_ZONE = (
    ("南卧室", "南卧室"), ("北书房", "北书房"), ("电梯间", "电梯间"),
    ("主卧", "主卧"), ("客卫", "客卫"), ("主卫", "主卫"), ("厨房", "厨房"),
    ("餐厅", "餐厅"), ("客厅", "客厅"), ("走廊", "走廊"), ("阳台", "阳台"),
    ("玄关", "玄关"), ("书房", "北书房"), ("南卧", "南卧室"),
)


def _panel_zone(eid, raw, area_map):
    head = _split_friendly(raw)[0]
    for kw, zone in _PANEL_ZONE:
        if kw in head:
            return zone
    return _zone(eid, raw, area_map)


def _is_lamp_switch(eid):
    if not _SWITCH_KEY_RE.search(eid):
        return False
    return eid.startswith(_LIGHT_SWITCH_PREFIX)


def _extract(states, area_map):
    env, climates, lamps, covers, persons = [], [], [], [], []
    openings, presence, leaks = [], [], []
    avail = 0
    for s in states:
        eid = s.get("entity_id") or ""
        dom, _, obj = eid.partition(".")
        st = s.get("state")
        a = s.get("attributes") or {}
        if st not in ("unavailable", "unknown"):
            avail += 1
        dc = a.get("device_class")
        raw = a.get("friendly_name") or ""

        if dom == "sensor" and dc in ("temperature", "humidity") and st not in ("unavailable", "unknown"):
            if any(k in raw for k in _ENV_SKIP):
                continue                      # 鱼缸水温/冰箱温度不是室温
            v = _num(st)
            if v is None:
                continue
            base = eid.rsplit("_", 1)[0]
            slot = next((e for e in env if e["_base"] == base), None)
            if not slot:
                slot = {"_base": base, "name": _friendly(s), "temp": None, "hum": None}
                env.append(slot)
            slot["hum" if dc == "humidity" else "temp"] = v

        elif dom == "climate":
            climates.append({
                "name": _friendly(s, 18),
                "area": _zone(eid, raw, area_map),
                "state": st,
                "off": st in ("off", "unavailable", "unknown"),
                "avail": st != "unavailable",
                "cur": _num(a.get("current_temperature")),
                "target": _num(a.get("temperature")),
                "mode": a.get("hvac_action") or a.get("hvac_mode") or "",
            })

        elif dom == "light":
            if eid.endswith(_IGNORE_LIGHT_SUFFIX):
                continue                      # 设备指示灯，不算照明
            icon = (a.get("icon") or "")
            is_group = ("_group_" in eid) or icon.endswith("lightbulb-group")
            if is_group:
                continue                      # 灯组是虚拟的，子灯才是真实状态
            b = a.get("brightness")
            lamps.append({
                "id": eid,
                "name": _lamp_name(raw, eid),
                "full": _split_friendly(raw)[0] or eid,
                "panel": "",
                "area": _zone(eid, raw, area_map),
                "on": st == "on",
                "kind": "lamp",
                "pct": round(b / 255 * 100) if isinstance(b, (int, float)) else None,
                "kelvin": a.get("color_temp_kelvin"),
            })

        elif dom == "switch" and _is_lamp_switch(eid):
            lamps.append({
                "id": eid,
                "name": _lamp_name(raw, eid),
                "full": _split_friendly(raw)[0] or eid,
                "panel": _split_friendly(raw)[0],
                "area": _panel_zone(eid, raw, area_map),
                "on": st == "on",
                "kind": "switch",
                "pct": None,
                "kelvin": None,
            })

        elif dom == "cover":
            pos = a.get("current_position")
            covers.append({
                "name": _friendly(s, 16),
                "area": _panel_zone(eid, raw, area_map),
                "state": st,
                "pos": pos if isinstance(pos, (int, float)) else None,
                "open": st == "open" or (isinstance(pos, (int, float)) and pos > 0),
                "avail": st not in ("unavailable", "unknown"),
            })

        elif dom == "person":
            persons.append({"name": _friendly(s), "state": st})

        elif dom == "binary_sensor":
            if st in ("unavailable", "unknown"):
                continue
            if dc in ("door", "window", "garage_door", "opening"):
                openings.append({
                    "name": _friendly(s, 16),
                    "area": _panel_zone(eid, raw, area_map),
                    "type": dc,
                    "open": st == "on",
                })
            elif dc in ("occupancy", "presence", "motion"):
                presence.append({
                    "name": _friendly(s, 16),
                    "area": _panel_zone(eid, raw, area_map),
                    "active": st == "on",
                })
            elif dc == "moisture":
                leaks.append({"name": _friendly(s, 16),
                              "area": _panel_zone(eid, raw, area_map),
                              "wet": st == "on"})

    # ---- 温湿度归并 ----
    for e in env:
        e.pop("_base", None)
        nm = e["name"]
        for suf in (" 温度", " 相对湿度", "_温度", "温度", "湿度"):
            if nm.endswith(suf):
                nm = nm[: -len(suf)]
                break
        e["name"] = nm.strip(" _-") or "传感器"
    merged = {}
    for e in env:
        k = e["name"]
        if k in merged:
            if e["temp"] is not None:
                merged[k]["temp"] = e["temp"]
            if e["hum"] is not None:
                merged[k]["hum"] = e["hum"]
        else:
            merged[k] = e
    env = sorted(merged.values(), key=lambda x: (x["temp"] is None, x["name"]))[:8]

    # ---- 灯：去重（中控屏同名虚拟键）+ 按房间排序 ----
    seen, uniq = set(), []
    for l in lamps:
        k = (l["area"], l["name"])
        if k in seen and l["kind"] == "switch":
            continue
        seen.add(k)
        uniq.append(l)
    uniq.sort(key=lambda x: (x["area"], x["name"]))

    by_area = {}
    for l in uniq:
        g = by_area.setdefault(l["area"], {"area": l["area"], "on": 0, "total": 0})
        g["total"] += 1
        if l["on"]:
            g["on"] += 1
    areas = sorted(by_area.values(), key=lambda x: (-x["on"], x["area"]))

    opens = [o for o in openings if o["open"]]
    return {
        "env": env,
        "climates": climates[:6],
        "lamps": uniq,
        "lamp_areas": areas,
        "lights_on": sum(1 for l in uniq if l["on"]),
        "lights_total": len(uniq),
        "covers": covers[:8],
        "covers_open": sum(1 for c in covers if c["open"]),
        "openings": openings[:12],
        "openings_on": len(opens),
        "has_door_sensor": bool(openings),
        "presence": presence[:10],
        "presence_on": sum(1 for p in presence if p["active"]),
        "leaks": leaks[:6],
        "persons": persons[:6],
        "available": avail,
        "total": len(states),
    }


def collect():
    out = {"ok": False, "ts": int(time.time())}
    try:
        token = _token()
        states = _get("/api/states", token)
        try:
            amap = _load_areas(token)
        except Exception:
            amap = _areas
        out.update(_extract(states, amap))

        # 室外天气
        for s in states:
            if s.get("entity_id", "").startswith("weather.") and s["state"] not in ("unavailable", "unknown"):
                a = s.get("attributes") or {}
                out["outdoor"] = {
                    "cond": s["state"],
                    "temp": _num(a.get("temperature")),
                    "hum": _num(a.get("humidity")),
                    "wind": _num(a.get("wind_speed")),
                    "name": a.get("friendly_name") or "室外",
                }
                break

        try:
            cfg = _get("/api/config", token)
            out["version"] = cfg.get("version")
            out["location"] = cfg.get("location_name")
        except Exception:
            pass
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


_load_tokens()

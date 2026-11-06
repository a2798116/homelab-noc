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

# 灯控藏在 switch 域的集成：智能开关面板、中控屏虚拟键
# 依据：面板按键实体形如 switch.xxx_on_p_2_1 →「走廊开关  走廊灯 左键」
# 前缀随各家设备不同，写 config.ha.lamp_switch_prefix；不配就只按实体名规则匹配，
# 这样代码里不会留下任何站点自己的设备编号。
_LIGHT_SWITCH_PREFIX = tuple(
    str(x).strip().lower() for x in (config.HA_LAMP_SWITCH_PREFIX or []) if str(x).strip()
)
_SWITCH_KEY_RE = re.compile(r"_on_p_\d+_\d+$")

# ---- 空调实体白名单 ----
# climate 域里不止空调：冰箱的冷藏/冷冻/变温间室、红外空调伴侣（插座），
# 以及同一台设备被 Xiaomi Home / Xiaomi Miot 两套集成各上报一次，都会混进来。
# 所以按 config.ha.climate_entities 点名，顺序即页面顺序；未配置时才走兜底过滤。
_CLIMATE_CFG = []
for _c in (config.HA_CLIMATE_ENTITIES or []):
    _eid = str(_c.get("entity_id") or "").strip()
    if _eid:
        _CLIMATE_CFG.append({
            "id": _eid,
            "name": str(_c.get("name") or "").strip(),
            "area": str(_c.get("area") or "").strip(),
        })
_CLIMATE_MAP = {c["id"]: c for c in _CLIMATE_CFG}
_CLIMATE_ORDER = {c["id"]: i for i, c in enumerate(_CLIMATE_CFG)}
_NON_AC_RE = re.compile(r"冰箱|冷藏|冷冻|变温|冰柜|插座|伴侣|热水器|水温", re.I)


def _climate_row(s, area_map):
    """把一条 climate 状态整理成「可展示 + 可控制」的结构"""
    eid = s.get("entity_id") or ""
    a = s.get("attributes") or {}
    st = s.get("state")
    raw = a.get("friendly_name") or ""
    cfg = _CLIMATE_MAP.get(eid) or {}
    modes = [str(m) for m in (a.get("hvac_modes") or []) if m and m != "off"]
    return {
        "id": eid,
        "name": cfg.get("name") or _friendly(s, 18),
        "area": cfg.get("area") or _zone(eid, raw, area_map),
        "state": st,
        "off": st in ("off", "unavailable", "unknown"),
        "avail": st not in ("unavailable", "unknown"),
        "cur": _num(a.get("current_temperature")),
        "target": _num(a.get("temperature")),
        "mode": a.get("hvac_action") or a.get("hvac_mode") or "",
        "hvac_mode": a.get("hvac_mode") or st,
        "hvac_modes": modes,
        "fan_mode": a.get("fan_mode"),
        "fan_modes": [str(x) for x in (a.get("fan_modes") or []) if x is not None],
        "preset_mode": a.get("preset_mode"),
        "preset_modes": [str(x) for x in (a.get("preset_modes") or []) if x is not None],
        "min": _num(a.get("min_temp")),
        "max": _num(a.get("max_temp")),
        "step": _num(a.get("target_temp_step")) or 1,
        "features": a.get("supported_features") or 0,
    }


def _climate_missing(cfg):
    """白名单里的实体这轮没上报：留一张离线卡，别让控制入口凭空消失"""
    return {
        "id": cfg["id"], "name": cfg["name"] or cfg["id"], "area": cfg["area"] or "",
        "state": "unavailable", "off": True, "avail": False,
        "cur": None, "target": None, "mode": "", "hvac_mode": "off",
        "hvac_modes": [], "fan_mode": None, "fan_modes": [],
        "preset_mode": None, "preset_modes": [], "min": None, "max": None,
        "step": 1, "features": 0,
    }

# 按键名尾巴，去掉后才是这路灯真正的名字
_KEY_SUFFIX = ("左键", "中键", "右键", "开关", "按键")
# 这些词单独留下来没有信息量，说明该用面板名
_GENERIC_NAME = ("开关", "按键", "灯", "左键", "中键", "右键")
# ---- 室内环境：以「房间」为单位，数据源在 config.local.json 里点名 ----
# HA 里叫「温度」的实体有几十个：群晖磁盘 68°C、冰箱 -20°C、浴霸 66°C 全都带
# device_class=temperature，按 device_class 一锅端出来的「室内 68°」纯属胡说。
# 所以客厅认 ZM1（MQTT）、书房认 Broadlink A2，写成实体白名单。
_EXTRA_LABEL = {
    "pm1": ("PM1", None),
    "pm25": ("PM2.5", None),
    "pm2_5": ("PM2.5", None),
    "pm10": ("PM10", None),
    # ZM1 的 hcho 在 HA 里标的是 μg/m³，但读数 0.04 这个量级只有 mg/m³ 讲得通
    # （0.04 mg/m³ ≈ 40 μg/m³，正常室内水平），按 mg/m³ 显示
    "hcho": ("甲醛", "mg/m³"),
    "ch2o": ("甲醛", "mg/m³"),
    "co2": ("CO₂", "ppm"),
    "tvoc": ("TVOC", None),
    "voc": ("VOC", None),
    "aqi": ("AQI", None),
    "lux": ("照度", "lx"),
    "noise": ("噪音", "dB"),
    "bat": ("电量", "%"),
}

# 自动兜底时，名字里带这些词的「温度」都不是室温（机柜/网络设备温度另算一类）
_ENV_NOT_ROOM = (
    "冰箱", "冰柜", "鱼缸", "水温", "热水", "cpu", "boss", "disk", "磁盘", "硬盘",
    "插座", "空调", "浴霸", "管温", "排气", "模块温度", "系统温度", "负载监控",
    "电池", "机箱", "交换机", "光猫", "水箱", "极空间",
)

# 顶栏「室内」优先显示哪个房间
_ENV_PRIMARY = "客厅"


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
        # 长效令牌优先：不过期（10 年），直接透传
        if config.HA_TOKEN:
            return config.HA_TOKEN
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
        # 模板里 area_name() 没有区域时渲染成字面量 "None"，别当房间名用
        if area in ("None", "none", "unknown", "未知", "无"):
            continue
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
    # 配了前缀就只认这些面板；没配就按实体名规则认，换设备不用改代码
    return eid.startswith(_LIGHT_SWITCH_PREFIX) if _LIGHT_SWITCH_PREFIX else True


# ---------------- 室内环境 ----------------
def _room_item(stats, room):
    """按配置里的实体白名单读一个房间：温度/湿度 + 若干空气质量指标"""
    ents = room.get("entities") or room.get("entity") or {}
    if not isinstance(ents, dict):
        return None
    item = {
        "name": (room.get("name") or "房间").strip(),
        "src": (room.get("src") or "").strip(),
        "temp": None, "hum": None, "extra": [],
    }
    for k, eid in ents.items():
        s = stats.get(eid) if isinstance(eid, str) else None
        if not s:
            continue
        if s.get("state") in (None, "unavailable", "unknown"):
            continue
        v = _num(s.get("state"))
        if v is None:
            continue
        key = str(k).lower()
        if key in ("temp", "t", "temperature"):
            item["temp"] = v
        elif key in ("hum", "h", "humidity", "rh"):
            item["hum"] = v
        else:
            label, unit = _EXTRA_LABEL.get(key, (str(k).upper(), None))
            unit = unit or (s.get("attributes") or {}).get("unit_of_measurement") or ""
            item["extra"].append({"k": label, "v": v, "u": unit})
    if item["temp"] is None and item["hum"] is None and not item["extra"]:
        return None
    return item


def _room_env(states, area_map):
    """室内读数 = 配置点名的房间（客厅 ZM1 / 书房 Broadlink）+ HA 区域里其它合规传感器

    兜底那部分只收「挂在区域里、单位是 °C、名字不像设备」的传感器：
    没挂区域的（同一只表被两个集成重复上报）和名字带 Boss/Disk/冰箱 的一律不要。
    """
    stats = {s.get("entity_id"): s for s in states if s.get("entity_id")}

    rooms, taken = [], set()
    for r in config.HA_ENV_ROOMS:
        if not isinstance(r, dict):
            continue
        it = _room_item(stats, r)
        if not it:
            continue
        rooms.append(it)
        taken.add(it["name"])

    # ---- 兜底：区域里剩下的温湿度传感器 ----
    by_area = {}
    for s in states:
        eid = s.get("entity_id") or ""
        dom = eid.split(".", 1)[0]
        if dom != "sensor":
            continue
        a = s.get("attributes") or {}
        dc = a.get("device_class")
        if dc not in ("temperature", "humidity"):
            continue
        if s.get("state") in (None, "unavailable", "unknown"):
            continue
        nm = (a.get("friendly_name") or eid).lower()
        if any(k in nm for k in _ENV_NOT_ROOM):
            continue
        unit = (a.get("unit_of_measurement") or "")
        if dc == "temperature" and "F" in unit.upper():
            continue                                  # 华氏的那几只都是设备内部温度
        v = _num(s.get("state"))
        if v is None:
            continue
        if dc == "temperature" and not (-10 <= v <= 55):
            continue                                  # 室温不可能跑到这个区间外
        area = area_map.get(eid)
        if not area or area in taken or area in ("网络", "其他"):
            continue
        slot = by_area.setdefault(area, {"name": area, "src": "HA 区域",
                                         "temp": None, "hum": None, "extra": []})
        slot["hum" if dc == "humidity" else "temp"] = v

    env = rooms + [v for k, v in by_area.items() if k not in taken]

    # 顶栏优先客厅：把它顶到第一位，前端不用猜
    env.sort(key=lambda x: (0 if x["name"] == _ENV_PRIMARY else 1,
                            0 if x["temp"] is not None else 1))
    return env[:8]


def _outdoor(states, stats):
    """室外：指定 weather.forecast_home；没配才退回第一个有温度的 weather.*"""
    pick = stats.get(config.HA_OUTDOOR_ENTITY) if config.HA_OUTDOOR_ENTITY else None
    if not pick or pick.get("state") in (None, "unavailable", "unknown"):
        pick = None
        for s in states:
            if not (s.get("entity_id") or "").startswith("weather."):
                continue
            if s.get("state") in (None, "unavailable", "unknown"):
                continue
            a0 = s.get("attributes") or {}
            if a0.get("temperature") is None:
                continue
            pick = s
            break
    if not pick:
        return None
    a = pick.get("attributes") or {}
    return {
        "cond": pick.get("state"),
        "temp": _num(a.get("temperature")),
        "hum": _num(a.get("humidity")),
        "wind": _num(a.get("wind_speed")),
        "wind_bearing": _num(a.get("wind_bearing")),
        "pressure": _num(a.get("pressure")),
        "dew": _num(a.get("dew_point")),
        "uv": _num(a.get("uv_index")),
        "vis": _num(a.get("visibility")),
        "name": a.get("friendly_name") or "室外",
        "entity": pick.get("entity_id"),
    }


# ---------------- 掉线时用上一次的有效读数 ----------------
# met.no 会整块变 unavailable、蓝牙温湿度计偶尔一两条 unknown。一次采样失败就把
# 顶栏打成「—」太蠢，这里记住上一份有效值补位，并带上 stale 秒数让界面说明。
_last = {"rooms": {}, "outdoor": None}
_last_file = ""
_last_persist = 0.0


def _last_path():
    global _last_file
    if _last_file:
        return _last_file
    base = os.path.dirname(config.HA_TOKEN_FILE or "/data/ha_token.json")
    _last_file = os.path.join(base or ".", "ha_last.json")
    try:
        with open(_last_file, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            _last["rooms"] = d.get("rooms") or {}
            _last["outdoor"] = d.get("outdoor")
    except Exception:
        pass
    return _last_file


def _last_save():
    """落盘：容器重启后仍能拿上一次的有效读数，不然第一轮采样就是「—」"""
    global _last_persist
    now = time.time()
    if now - _last_persist < 120:
        return
    _last_persist = now
    p = _last_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"rooms": _last["rooms"], "outdoor": _last["outdoor"]}, f, ensure_ascii=False)
    except OSError:
        pass


def _sensor_outdoor(stats):
    """天气实体整块挂掉时，退到配置里的备用传感器（空调外机环境温度）"""
    for f in config.HA_OUTDOOR_FALLBACK:
        eid = f.get("entity") if isinstance(f, dict) else f
        s = stats.get(eid) if isinstance(eid, str) else None
        if not s or s.get("state") in (None, "unavailable", "unknown"):
            continue
        v = _num(s.get("state"))
        if v is None or not (-40 <= v <= 60):
            continue
        unit = (s.get("attributes") or {}).get("unit_of_measurement") or ""
        if "F" in unit.upper():
            v = (v - 32) * 5.0 / 9.0
        src = (f.get("src") if isinstance(f, dict) else "") or "备用传感器"
        short = (f.get("short") if isinstance(f, dict) else "") or src
        for suf in ("环境温度", "温度"):
            if short.endswith(suf):
                short = short[:-len(suf)]
        return {"cond": None, "temp": v, "hum": None, "wind": None,
                "dew": None, "uv": None, "pressure": None, "vis": None,
                "name": src, "src": src, "short": short or src, "entity": eid}
    return None


def _carry(env, outdoor):
    now = time.time()
    _last_path()

    # 1) 点名房间整个消失在 env 里（实体全 unavailable）→ 用缓存补回来
    names = {e.get("name") for e in env}
    for r in config.HA_ENV_ROOMS:
        if not isinstance(r, dict):
            continue
        nm = (r.get("name") or "").strip()
        prev = _last["rooms"].get(nm)
        if nm and nm not in names and prev:
            it = {"name": nm, "src": prev.get("src") or (r.get("src") or ""),
                  "temp": prev.get("temp"), "hum": prev.get("hum"),
                  "extra": [dict(x) for x in prev.get("extra") or []],
                  "stale": int(now - prev.get("ts", now))}
            env.append(it)
            names.add(nm)

    # 2) 单字段缺失 → 逐项补
    for it in env:
        prev = _last["rooms"].get(it.get("name")) or {}
        fresh = False
        if not prev:
            if it.get("temp") is not None or it.get("hum") is not None:
                _last["rooms"][it.get("name")] = {
                    "src": it.get("src"), "temp": it.get("temp"), "hum": it.get("hum"),
                    "extra": [dict(x) for x in it.get("extra") or []], "ts": now}
            continue
        for k in ("temp", "hum"):
            if it.get(k) is None and prev.get(k) is not None:
                it[k] = prev[k]
                it["stale"] = int(now - prev.get("ts", now))
            elif it.get(k) is not None:
                fresh = True
        if not it.get("extra") and prev.get("extra"):
            it["extra"] = [dict(x) for x in prev["extra"]]
            it.setdefault("stale", int(now - prev.get("ts", now)))
        elif it.get("extra"):
            fresh = True
        if fresh:
            _last["rooms"][it.get("name")] = {
                "src": it.get("src"), "temp": it.get("temp"), "hum": it.get("hum"),
                "extra": [dict(x) for x in it.get("extra") or []], "ts": now}

    # 3) 室外
    if not outdoor or outdoor.get("temp") is None:
        prev = _last.get("outdoor")
        if prev:
            outdoor = dict(prev)
            outdoor["stale"] = int(now - prev.get("ts", now))
    else:
        _last["outdoor"] = dict(outdoor, ts=now)
    _last_save()
    return env, outdoor



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

        if dom == "climate":
            if _CLIMATE_MAP:
                if eid not in _CLIMATE_MAP:
                    continue
            else:
                # 没配白名单时的兜底：排除冰箱间室、红外空调伴侣和自检温度缺失的虚拟实体
                lo = _num(a.get("min_temp"))
                if (_NON_AC_RE.search(raw) or (lo is not None and lo < 0)
                        or a.get("current_temperature") is None):
                    continue
            row = _climate_row(s, area_map)
            # 同一房间的设备被多个集成重复上报时，留能力最全的那条
            dup = next((c for c in climates if c["area"] == row["area"]), None)
            if dup:
                if row["features"] > dup["features"]:
                    climates[climates.index(dup)] = row
                continue
            climates.append(row)

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

    # ---- 室内环境：点名房间优先，其余按 HA 区域兜底 ----
    env = _room_env(states, area_map)

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

    # ---- 空调：按点名顺序输出，白名单里缺的补一张离线卡 ----
    if _CLIMATE_MAP:
        have = {c["id"] for c in climates}
        for c in _CLIMATE_CFG:
            if c["id"] not in have:
                climates.append(_climate_missing(c))
        climates.sort(key=lambda c: _CLIMATE_ORDER.get(c["id"], 999))
    opens = [o for o in openings if o["open"]]
    return {
        "env": env,
        "climates": climates[:12],
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

        # 室外天气：钉死 weather.forecast_home（met.no），别再被别的 weather 实体抢走
        stats = {s.get("entity_id"): s for s in states}
        od = _outdoor(states, stats)
        env, od = _carry(out.get("env") or [], od)
        out["env"] = env
        if not od or od.get("temp") is None:
            od = _sensor_outdoor(stats) or od      # met.no 整块挂掉时的最后兜底
        if od:
            out["outdoor"] = od

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


# ---------------- 反向控制 ----------------
_CLIMATE_SERVICES = {
    "set_temperature": "set_temperature",
    "set_hvac_mode": "set_hvac_mode",
    "set_preset_mode": "set_preset_mode",
    "set_fan_mode": "set_fan_mode",
}


def call_climate(entity_id, action, value=None, row=None):
    """把面板上的一次点击翻译成 HA 的 climate 服务调用。

    只接受白名单实体和上面四种服务；模式/预设/风速的取值必须来自设备自己
    上报的枚举，温度限制在设备上报的范围内 —— 不做成任意服务调用入口。
    """
    entity_id = str(entity_id or "")
    action = str(action or "")
    row = row or {}
    if not _CLIMATE_MAP or entity_id not in _CLIMATE_MAP:
        return False, "实体不在空调白名单内"
    if not config.HA_URL:
        return False, "HA_URL 未配置"

    data = {"entity_id": entity_id}
    if action == "set_temperature":
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False, "温度不是数字"
        lo, hi = _num(row.get("min")), _num(row.get("max"))
        if lo is not None and v < lo:
            return False, "低于设备最低温度"
        if hi is not None and v > hi:
            return False, "高于设备最高温度"
        data["temperature"] = v
    elif action in ("set_hvac_mode", "set_preset_mode", "set_fan_mode"):
        key = {"set_hvac_mode": "hvac_modes",
               "set_preset_mode": "preset_modes",
               "set_fan_mode": "fan_modes"}[action]
        allowed = [str(x) for x in (row.get(key) or [])]
        if action == "set_hvac_mode" and str(value) == "off":
            allowed.append("off")
        if str(value) not in allowed:
            return False, "取值不在设备支持的列表内"
        data[key[:-1]] = str(value)
    else:
        return False, "不支持的操作"

    # 只有 auto/off 的浴霸，点「制热 / 换气」得先开机再切预设；
    # 「待机」这类关闭语义、以及有完整模式列表的空调都不自动开机。
    _off_preset = str(value).strip().lower() in ("待机", "idle", "none", "off", "关闭")
    if (action == "set_preset_mode" and row.get("off") and not _off_preset
            and len(row.get("hvac_modes") or []) == 1 and (row.get("hvac_modes") or [])):
        boot = {"entity_id": entity_id, "hvac_mode": str(row["hvac_modes"][0])}
    else:
        boot = None
    try:
        token = _token()
        if boot:
            st, res = _post_json("/api/services/climate/set_hvac_mode", boot, token, timeout=10)
            if st not in (200, 201, 202):
                return False, "开机失败 %s" % st
        st, res = _post_json("/api/services/climate/" + _CLIMATE_SERVICES[action],
                             data, token, timeout=10)
        if st >= 500:
            # 部分集成在设备关机时拒绝温度/风速设置，会直接抛 500
            return False, "设备拒绝了设置，请先开机或稍后重试"
        if st not in (200, 201, 202):
            return False, "HA 返回 %s %s" % (st, str(res)[:100])
    except Exception as e:
        return False, "调用失败 %s" % str(e)[:120]
    return True, ""

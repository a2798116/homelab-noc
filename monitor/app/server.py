#!/usr/bin/env python3
"""Homelab Monitor - 家庭设备状态看板后端

纯标准库实现：ThreadingHTTPServer + sqlite3，无第三方依赖。
数据来自各设备定期 POST 的上报，前端页面通过 REST 读取。
"""
import base64
import hmac
import ipaddress
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DB_PATH = os.environ.get("DB_PATH", "/data/monitor.db")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
AUTH_USER = os.environ.get("AUTH_USER", "")
AUTH_PASS = os.environ.get("AUTH_PASS", "")
PORT = int(os.environ.get("PORT", "8930"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
INIT_DEVICES = os.environ.get("INIT_DEVICES", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# ---------- 来源控制 ----------
# 受信来源（内网）可访问全部路径；非受信来源（公网）只允许 PUBLIC_PATHS。
TRUSTED_NETS = os.environ.get(
    "TRUSTED_NETS", "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
)
# 公网可访问的路径：上报接口 + 健康检查 + 采集端安装包
PUBLIC_PATHS = os.environ.get(
    "PUBLIC_PATHS", "/api/report,/api/health,/agent/install.sh,/agent/fnos-monitor-report.py"
)
# 免 Basic Auth 的路径：上报靠设备令牌鉴权，安装包本身无敏感信息
NO_AUTH_PATHS = os.environ.get(
    "NO_AUTH_PATHS", "/api/health,/api/report,/agent/install.sh,/agent/fnos-monitor-report.py"
)
# 是否信任反向代理传来的 X-Forwarded-For（直连公网时必须为 0，否则可伪造绕过）
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
# 强制不信任的来源 IP。路由器做端口映射时若开了源地址转换，
# 公网请求到达这里时源 IP 会变成网关地址，必须显式排除。
UNTRUSTED_SRC = set(
    ip.strip() for ip in os.environ.get("UNTRUSTED_SRC", "").split(",") if ip.strip()
)


def _parse_nets(spec):
    nets = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            print("[warn] 忽略无效网段:", item)
    return nets


def _parse_paths(spec):
    return [p.strip() for p in spec.split(",") if p.strip()]


TRUSTED = _parse_nets(TRUSTED_NETS)
PUBLIC = _parse_paths(PUBLIC_PATHS)
NO_AUTH = _parse_paths(NO_AUTH_PATHS)

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token TEXT NOT NULL UNIQUE,
  meta TEXT,
  created_at INTEGER NOT NULL,
  last_seen INTEGER
);
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  cpu_temp REAL,
  nvme_temp REAL,
  pch_temp REAL,
  wifi_temp REAL,
  board_temp REAL,
  cpu_usage REAL,
  load1 REAL,
  load5 REAL,
  load15 REAL,
  mem_used INTEGER,
  mem_total INTEGER,
  swap_used INTEGER,
  swap_total INTEGER,
  uptime INTEGER,
  disks TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_dev_ts ON reports(device_id, ts);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
    if INIT_DEVICES:
        try:
            items = json.loads(INIT_DEVICES)
        except Exception as exc:  # noqa: BLE001
            print("[warn] INIT_DEVICES 解析失败:", exc)
            items = []
        with connect() as conn:
            for d in items:
                did = d.get("id")
                token = d.get("token")
                if not did or not token:
                    continue
                conn.execute(
                    "INSERT INTO devices(id,name,token,meta,created_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, token=excluded.token, meta=excluded.meta",
                    (did, d.get("name", did), token, json.dumps(d.get("meta") or {}, ensure_ascii=False), int(time.time())),
                )
        print("[init] 已同步 %d 个设备" % len(items))


def prune():
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    with connect() as conn:
        cur = conn.execute("DELETE FROM reports WHERE ts < ?", (cutoff,))
        if cur.rowcount:
            print("[prune] 清理 %d 条过期记录" % cur.rowcount)


def device_by_token(conn, token):
    row = conn.execute("SELECT id, name FROM devices WHERE token = ?", (token,)).fetchone()
    if not row:
        return None
    if not hmac.compare_digest(str(row["id"]), str(row["id"])):
        return None
    return row


def num(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def intv(v, default=None):
    try:
        if v is None:
            return default
        return int(v)
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "homelab-monitor/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 精简日志
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---------- 响应工具 ----------
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, code=200, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = intv(self.headers.get("Content-Length"), 0) or 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    # ---------- 鉴权 ----------
    def client_ip(self):
        if TRUST_PROXY:
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def is_trusted(self):
        """来源是否为内网/受信网段。"""
        ip = self.client_ip()
        if ip in UNTRUSTED_SRC:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in TRUSTED)

    def path_allowed(self, path):
        """非受信来源（公网）只放行 PUBLIC 白名单内的路径。"""
        if self.is_trusted():
            return True
        if path in PUBLIC:
            return True
        for p in PUBLIC:
            if p.endswith("*") and path.startswith(p[:-1]):
                return True
        return False

    def deny_public(self):
        body = json.dumps(
            {"error": "forbidden", "hint": "面板与查询接口仅限内网访问，公网仅开放上报通道"}
        ).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        """未配置 AUTH_USER 时不启用认证；否则校验 HTTP Basic。"""
        if not AUTH_USER:
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(hdr[6:].strip()).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return False
        user, sep, pwd = raw.partition(":")
        if not sep:
            return False
        return hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(pwd, AUTH_PASS)

    def require_auth(self):
        # 内网/隧道来源直接放行：面板设计上只对内网开放，
        # 公网来源在 path_allowed() 阶段已被 403 拦掉，到不了这里。
        if self.is_trusted():
            return True
        if self.authorized():
            return True
        body = b'{"error":"unauthorized"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Homelab Monitor", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Device-Token, X-Admin-Token, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)
        # 公网来源只能碰白名单路径
        if not self.path_allowed(path):
            return self.deny_public()
        # 白名单路径免认证；其余需要 Basic Auth
        if path not in NO_AUTH and not self.require_auth():
            return
        try:
            if path == "/api/health":
                return self.send_json({
                    "ok": True,
                    "time": int(time.time()),
                    "trusted": self.is_trusted(),
                    "src": self.client_ip() if self.is_trusted() else "hidden",
                })
            if path == "/api/devices":
                return self.api_devices()
            if path.startswith("/api/devices/") and path.endswith("/history"):
                did = path.split("/")[3]
                hours = intv((q.get("hours") or ["24"])[0], 24)
                return self.api_history(did, hours)
            if path.startswith("/api/devices/") and path.endswith("/latest"):
                did = path.split("/")[3]
                return self.api_latest(did)
            return self.serve_static(path)
        except Exception as exc:  # noqa: BLE001
            print("[error]", exc)
            return self.send_json({"error": str(exc)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        if not self.path_allowed(path):
            return self.deny_public()
        if path not in NO_AUTH and not self.require_auth():
            return
        try:
            if path == "/api/report":
                return self.api_report()
            if path == "/api/devices":
                return self.api_create_device()
            return self.send_json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            print("[error]", exc)
            return self.send_json({"error": str(exc)}, 500)

    # ---------- 业务接口 ----------
    def api_report(self):
        token = self.headers.get("X-Device-Token", "")
        if not token:
            return self.send_json({"error": "missing X-Device-Token"}, 401)
        raw = self.read_body()
        try:
            data = json.loads(raw or b"{}")
        except Exception:  # noqa: BLE001
            return self.send_json({"error": "invalid json"}, 400)
        with connect() as conn:
            dev = device_by_token(conn, token)
            if not dev:
                return self.send_json({"error": "invalid token"}, 403)
            did = dev["id"]
            ts = intv(data.get("ts"), int(time.time()))
            temps = data.get("temps") or {}
            cpu = data.get("cpu") or {}
            mem = data.get("mem") or {}
            disks = data.get("disks") or []
            conn.execute(
                "INSERT INTO reports(device_id,ts,cpu_temp,nvme_temp,pch_temp,wifi_temp,board_temp,"
                "cpu_usage,load1,load5,load15,mem_used,mem_total,swap_used,swap_total,uptime,disks,detail)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    did, ts,
                    num(temps.get("cpu")), num(temps.get("nvme")), num(temps.get("pch")),
                    num(temps.get("wifi")), num(temps.get("board")),
                    num(cpu.get("usage")), num(cpu.get("load1")), num(cpu.get("load5")), num(cpu.get("load15")),
                    intv(mem.get("used")), intv(mem.get("total")), intv(mem.get("swap_used")), intv(mem.get("swap_total")),
                    intv(data.get("uptime")), json.dumps(disks, ensure_ascii=False),
                    json.dumps(data, ensure_ascii=False),
                ),
            )
            meta = data.get("meta")
            if isinstance(meta, dict) and meta:
                conn.execute(
                    "UPDATE devices SET last_seen=?, meta=? WHERE id=?",
                    (ts, json.dumps(meta, ensure_ascii=False), did),
                )
            else:
                conn.execute("UPDATE devices SET last_seen=? WHERE id=?", (ts, did))
        return self.send_json({"ok": True, "device": did, "ts": ts})

    def api_devices(self):
        with connect() as conn:
            rows = conn.execute("SELECT id,name,meta,created_at,last_seen FROM devices ORDER BY name").fetchall()
            out = []
            now = int(time.time())
            for r in rows:
                latest = conn.execute(
                    "SELECT * FROM reports WHERE device_id=? ORDER BY ts DESC LIMIT 1", (r["id"],)
                ).fetchone()
                item = {
                    "id": r["id"],
                    "name": r["name"],
                    "meta": json.loads(r["meta"]) if r["meta"] else {},
                    "last_seen": r["last_seen"],
                    "online": bool(r["last_seen"] and now - r["last_seen"] < 600),
                    "latest": dict(latest) if latest else None,
                }
                if item["latest"]:
                    item["latest"]["disks"] = json.loads(item["latest"].get("disks") or "[]")
                    item["latest"]["detail"] = json.loads(item["latest"].get("detail") or "{}")
                out.append(item)
        return self.send_json({"devices": out, "now": now})

    def api_latest(self, did):
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM reports WHERE device_id=? ORDER BY ts DESC LIMIT 1", (did,)
            ).fetchone()
        if not row:
            return self.send_json({"error": "no data"}, 404)
        d = dict(row)
        d["disks"] = json.loads(d.get("disks") or "[]")
        d["detail"] = json.loads(d.get("detail") or "{}")
        return self.send_json(d)

    def api_history(self, did, hours):
        since = int(time.time()) - hours * 3600
        with connect() as conn:
            rows = conn.execute(
                "SELECT ts,cpu_temp,nvme_temp,pch_temp,wifi_temp,cpu_usage,load1,mem_used,mem_total "
                "FROM reports WHERE device_id=? AND ts>=? ORDER BY ts ASC LIMIT 5000",
                (did, since),
            ).fetchall()
        return self.send_json({"device": did, "hours": hours, "points": [dict(r) for r in rows]})

    def api_create_device(self):
        if not ADMIN_TOKEN:
            return self.send_json({"error": "admin disabled"}, 403)
        if self.headers.get("X-Admin-Token", "") != ADMIN_TOKEN:
            return self.send_json({"error": "invalid admin token"}, 403)
        data = json.loads(self.read_body() or b"{}")
        did, name, token = data.get("id"), data.get("name"), data.get("token")
        if not (did and name and token):
            return self.send_json({"error": "id/name/token required"}, 400)
        with connect() as conn:
            conn.execute(
                "INSERT INTO devices(id,name,token,meta,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, token=excluded.token",
                (did, name, token, json.dumps(data.get("meta") or {}, ensure_ascii=False), int(time.time())),
            )
        return self.send_json({"ok": True, "id": did})

    # ---------- 静态文件 ----------
    def serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        safe = os.path.normpath(path).lstrip("/\\")
        full = os.path.join(STATIC_DIR, safe)
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            return self.send_text("Not Found", 404)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".sh": "text/x-shellscript; charset=utf-8",
            ".py": "text/x-python; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def housekeeping():
    prune()
    while True:
        time.sleep(6 * 3600)
        try:
            prune()
        except Exception as exc:  # noqa: BLE001
            print("[prune error]", exc)


def main():
    init_db()
    threading.Thread(target=housekeeping, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("[start] listening on 0.0.0.0:%d, db=%s, retention=%dd" % (PORT, DB_PATH, RETENTION_DAYS))
    srv.serve_forever()


if __name__ == "__main__":
    main()

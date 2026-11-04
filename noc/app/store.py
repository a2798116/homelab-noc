# -*- coding: utf-8 -*-
"""NOC 的持久化层：事件流落 SQLite，容器重建也不丢

数据库就放在飞牛的 /data 卷上（和 config.local.json 同一个挂载），
不引入外部数据库服务——NOC 自己读写的量级，SQLite 足够，也省得再多一个要维护的东西。

表结构很朴素：
  events  一条事件一行，ts 是 unix 秒，按 ts 倒序查
  metrics 每 POLL_SAMPLE 秒采样一次关键指标，用来回溯"昨天这个时候流量多少"
"""
import os
import sqlite3
import threading

_lock = threading.RLock()
_conn = None
_path = ""

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     INTEGER NOT NULL,
  kind   TEXT,
  level  TEXT,
  text   TEXT NOT NULL,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

CREATE TABLE IF NOT EXISTS metrics (
  ts        INTEGER PRIMARY KEY,
  wan_up    REAL DEFAULT 0,
  wan_down  REAL DEFAULT 0,
  online    INTEGER DEFAULT 0,
  wifi      INTEGER DEFAULT 0,
  cpu       REAL DEFAULT 0,
  mem       REAL DEFAULT 0
);
"""


def init(path):
    """打开（必要时创建）数据库；失败只影响历史回溯，不影响实时面板"""
    global _conn, _path
    with _lock:
        _path = path
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            _conn = sqlite3.connect(path, check_same_thread=False, timeout=8)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.commit()
        except Exception:
            _conn = None
        return _conn is not None


def available():
    return _conn is not None


def path():
    return _path


# ---------------------------------------------------------------- 事件

def add_event(ev):
    """写一条事件。数据库没打开就静默丢弃，实时面板照常走内存队列。"""
    if _conn is None:
        return
    with _lock:
        try:
            _conn.execute(
                "INSERT INTO events (ts, kind, level, text, detail) VALUES (?,?,?,?,?)",
                (int(ev.get("t") or 0), ev.get("kind") or "", ev.get("level") or "info",
                 ev.get("text") or "", ev.get("detail") or ""),
            )
            _conn.commit()
        except Exception:
            pass


def recent_events(limit=300):
    """启动时把最近的事件读回内存，重启后事件流不会从空白开始"""
    if _conn is None:
        return []
    with _lock:
        try:
            rows = _conn.execute(
                "SELECT ts, kind, level, text, detail FROM events ORDER BY ts DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        except Exception:
            return []
    return [{"t": r["ts"], "kind": r["kind"], "level": r["level"],
             "text": r["text"], "detail": r["detail"]} for r in rows]


def query_events(limit=200, hours=24, level="", q=""):
    """按时间窗 / 级别 / 关键字翻历史"""
    if _conn is None:
        return []
    sql = ["SELECT ts, kind, level, text, detail FROM events WHERE 1=1"]
    args = []
    if hours and float(hours) > 0:
        sql.append("AND ts >= ?")
        args.append(int(__import__("time").time() - float(hours) * 3600))
    if level:
        sql.append("AND level = ?")
        args.append(level)
    if q:
        sql.append("AND (text LIKE ? OR detail LIKE ?)")
        args.extend(["%" + q + "%", "%" + q + "%"])
    sql.append("ORDER BY ts DESC, id DESC LIMIT ?")
    args.append(int(limit))

    with _lock:
        try:
            rows = _conn.execute(" ".join(sql), args).fetchall()
        except Exception:
            return []
    return [{"t": r["ts"], "kind": r["kind"], "level": r["level"],
             "text": r["text"], "detail": r["detail"]} for r in rows]


def event_days():
    """按天统计条数，给历史面板做"哪天有事"的刻度"""
    if _conn is None:
        return []
    with _lock:
        try:
            rows = _conn.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') AS d, COUNT(*) AS n, "
                "SUM(CASE WHEN level='crit' THEN 1 ELSE 0 END) AS crit, "
                "SUM(CASE WHEN level='warn' THEN 1 ELSE 0 END) AS warn "
                "FROM events GROUP BY d ORDER BY d DESC LIMIT 30"
            ).fetchall()
        except Exception:
            return []
    return [{"day": r["d"], "n": r["n"], "crit": r["crit"] or 0, "warn": r["warn"] or 0} for r in rows]


# ---------------------------------------------------------------- 指标

def add_metric(ts, wan_up=0, wan_down=0, online=0, wifi=0, cpu=0, mem=0):
    if _conn is None:
        return
    with _lock:
        try:
            _conn.execute(
                "INSERT OR REPLACE INTO metrics (ts, wan_up, wan_down, online, wifi, cpu, mem) "
                "VALUES (?,?,?,?,?,?,?)",
                (int(ts), float(wan_up or 0), float(wan_down or 0),
                 int(online or 0), int(wifi or 0), float(cpu or 0), float(mem or 0)),
            )
            _conn.commit()
        except Exception:
            pass


def query_metrics(hours=24, step=60):
    """按 step 秒聚合，避免前端一口气拿几万个点。

    刚部署时库里的点很少，按 5 分钟聚合可能只剩一两个点、曲线根本不成形，
    所以点数不够就自动往细里降，最低到单条采样（60 秒）。
    """
    if _conn is None:
        return []
    span = int(float(hours) * 3600)
    cand = [int(step)]
    s = int(step)
    while s > 60:
        s = max(60, s // 3)
        cand.append(s)

    rows = []
    with _lock:
        for s in cand:
            try:
                rows = _conn.execute(
                    "SELECT (ts / ?) * ? AS bucket, AVG(wan_up) AS up, AVG(wan_down) AS down, "
                    "MAX(online) AS online, AVG(cpu) AS cpu, AVG(mem) AS mem "
                    "FROM metrics WHERE ts >= strftime('%s','now') - ? "
                    "GROUP BY bucket ORDER BY bucket ASC",
                    (int(s), int(s), span),
                ).fetchall()
            except Exception:
                return []
            if len(rows) >= 24:
                break
    return [{"t": r["bucket"], "up": round(r["up"] or 0, 1), "down": round(r["down"] or 0, 1),
             "online": r["online"] or 0, "cpu": round(r["cpu"] or 0, 1),
             "mem": round(r["mem"] or 0, 1)} for r in rows]


# ---------------------------------------------------------------- 维护

def prune(days=90):
    """只留最近 N 天，飞牛那块盘不必为一个面板无限长"""
    if _conn is None:
        return 0
    cutoff = int(__import__("time").time() - float(days) * 86400)
    with _lock:
        try:
            n = _conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
            _conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
            _conn.commit()
            return n
        except Exception:
            return 0


def stats():
    if _conn is None:
        return {"ok": False}
    with _lock:
        try:
            e = _conn.execute("SELECT COUNT(*) c, MIN(ts) a, MAX(ts) b FROM events").fetchone()
            m = _conn.execute("SELECT COUNT(*) c FROM metrics").fetchone()
            size = os.path.getsize(_path) if _path and os.path.exists(_path) else 0
        except Exception:
            return {"ok": False}
    return {"ok": True, "path": _path, "events": e["c"], "since": e["a"], "until": e["b"],
            "metrics": m["c"], "bytes": size}

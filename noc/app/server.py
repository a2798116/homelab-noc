# -*- coding: utf-8 -*-
"""Homelab NOC — 全屋状态大屏服务端

零第三方依赖，仅标准库。采集线程 + HTTP + SSE。
"""
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import state
import store
from collectors import ha, ikuai, localnet, miwifi, pinger, pve, switch, fnos

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


def _loop(name, interval, fn):
    def run():
        while True:
            t0 = time.time()
            try:
                fn()
            except Exception:
                state.set_error(name, traceback.format_exc(limit=2))
                print("[%s] %s" % (name, traceback.format_exc(limit=2)), flush=True)
            time.sleep(max(interval - (time.time() - t0), 0.3))
    th = threading.Thread(target=run, name=name, daemon=True)
    th.start()
    return th


def job_fast():
    state.update("ikuai", ikuai.collect_system())
    ifaces = ikuai.collect_interfaces()
    state.update("ifaces", {k: v for k, v in ifaces.items() if k != "_phy"})
    state.update("wan", ifaces.get("wan1"))
    state.update("lan", ifaces.get("lan1"))
    state.update("local", localnet.collect())
    # 紧跟着合并线路检测字段，避免被下一轮 job_fast 的原始接口数据覆盖
    _merge_wan_check()


def job_devices():
    state.update_devices(ikuai.collect_devices())


def job_pve():
    state.update("pve", pve.collect())


def job_fnos():
    if not config.FNOS_HOST:
        return  # 未配置远程飞牛时静默跳过
    state.update("fnos", fnos.collect())


def job_probes():
    state.update("probes", pinger.collect())


def job_wan():
    state.update("wan_check", ikuai.collect_wan_check())


def job_switch():
    if not config.SWITCH_URL:
        return  # 未配置交换机管理地址时静默跳过，避免反复刷错误栈
    state.update_switch(switch.collect())


def job_wifi():
    state.update_wifi(miwifi.collect())


def job_vpn():
    state.update_vpn(ikuai.collect_vpn())


def job_ha():
    state.update_ha(ha.collect())


def _merge_wan_check():
    chk = state._state.get("wan_check") or {}
    wan = state._state.get("wan") or {}
    if wan and chk:
        wan = dict(wan)
        wan["ok"] = chk.get("ok", True)
        wan["msg"] = chk.get("msg", "")
        wan["public_ip"] = chk.get("ip")
        state.update("wan", wan)
    # 双 WAN：wan2 独立成状态（速率从 ifaces.wan2 取，前端合并渲染）
    lines = chk.get("lines") or []
    w2chk = next((l for l in lines if l.get("iface") == "wan2"), None)
    if w2chk:
        ifaces = state._state.get("ifaces") or {}
        f2 = ifaces.get("wan2") or {}
        state.update("wan2", {
            "iface": "wan2",
            "ip": w2chk.get("ip"),
            "gateway": w2chk.get("gateway"),
            "internet": w2chk.get("internet"),
            "ok": w2chk.get("ok", True),
            "msg": w2chk.get("msg", ""),
            "up": f2.get("up", 0),
            "down": f2.get("down", 0),
        })


def job_history():
    state.snapshot_history()


def job_metrics():
    """每 POLL_SAMPLE 秒采一条指标，作为"昨天这个时候"的回溯依据"""
    state.sample_metrics()


_last_prune = [0.0]


def job_prune():
    """一天清一次过期数据，别让库无限长"""
    now = time.time()
    if now - _last_prune[0] < 86400:
        return
    _last_prune[0] = now
    store.prune(config.DB_KEEP_DAYS)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, default=str)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        if rel == "static":
            rel = "index.html"
        full = os.path.normpath(os.path.join(STATIC_DIR, rel.replace("static/", "", 1)))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, MIME.get(ext, "application/octet-stream"))

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)

        if p == "/api/snapshot":
            return self._send(200, state.full_snapshot())
        if p == "/api/history":
            minutes = float((q.get("minutes") or ["30"])[0])
            return self._send(200, state.history(minutes))
        if p == "/api/events":
            return self._send(200, state.events(int((q.get("limit") or ["50"])[0])))
        if p == "/api/events/history":
            return self._send(200, state.event_history(
                int((q.get("limit") or ["200"])[0]),
                float((q.get("hours") or ["24"])[0]),
                (q.get("level") or [""])[0],
                (q.get("q") or [""])[0],
            ))
        if p == "/api/events/days":
            return self._send(200, state.event_days())
        if p == "/api/metrics":
            return self._send(200, state.metric_history(
                float((q.get("hours") or ["24"])[0]),
                int((q.get("step") or ["60"])[0]),
            ))
        if p == "/api/store":
            return self._send(200, store.stats())
        if p == "/api/health":
            return self._send(200, {"ok": True, "uptime": int(time.time() - state._state["started"])})
        if p == "/api/stream":
            return self._sse()
        return self._static(p if p != "/" else "/index.html")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/nas-report":
            return self._send(404, {"error": "not found"})
        # 设备侧只认固定令牌；留空则拒绝，避免裸奔
        if not config.NAS_REPORT_TOKEN:
            return self._send(503, {"error": "NAS_REPORT_TOKEN 未配置"})
        if self.headers.get("X-Noc-Token", "") != config.NAS_REPORT_TOKEN:
            return self._send(403, {"error": "token mismatch"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                return self._send(413, {"error": "payload too large"})
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return self._send(400, {"error": "invalid json"})
        if not isinstance(payload, dict):
            return self._send(400, {"error": "invalid payload"})
        state.update_nas_report(payload)
        return self._send(200, {"ok": True})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                snap = state.full_snapshot()
                payload = json.dumps(snap, ensure_ascii=False, default=str)
                self.wfile.write(("data: %s\n\n" % payload).encode("utf-8"))
                self.wfile.flush()
                time.sleep(1.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def main():
    print("=" * 58, flush=True)
    print(" Homelab NOC  http://%s:%d" % (config.HOST, config.PORT), flush=True)
    print(" iKuai: %s   PVE: %s" % (config.IKUAI_URL, config.PVE_URL), flush=True)
    print("=" * 58, flush=True)

    if state.init_store():
        st = store.stats()
        print(" 事件库: %s（%d 条历史事件）" % (st.get("path"), st.get("events") or 0), flush=True)
    else:
        print(" 事件库: 打开失败，历史回溯不可用（实时事件不受影响）", flush=True)

    _loop("ikuai", config.POLL_FAST, job_fast)
    _loop("devices", config.POLL_DEVICES, job_devices)
    _loop("pve", config.POLL_PVE, job_pve)
    _loop("fnos", config.POLL_FNOS, job_fnos)
    _loop("probes", config.POLL_PING, job_probes)
    _loop("switch", config.POLL_SWITCH, job_switch)
    _loop("miwifi", config.POLL_MIWIFI, job_wifi)
    _loop("vpn", config.POLL_VPN, job_vpn)
    _loop("ha", config.POLL_HA, job_ha)
    _loop("wancheck", 30, job_wan)
    _loop("wanmerge", 30, _merge_wan_check)
    _loop("history", config.POLL_FAST, job_history)
    _loop("metrics", config.POLL_SAMPLE, job_metrics)
    _loop("prune", 3600, job_prune)

    time.sleep(3)
    srv = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()

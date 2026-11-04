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
from collectors import ha, ikuai, localnet, miwifi, pinger, pve, switch

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


def job_devices():
    state.update_devices(ikuai.collect_devices())


def job_pve():
    state.update("pve", pve.collect())


def job_probes():
    state.update("probes", pinger.collect())


def job_wan():
    state.update("wan_check", ikuai.collect_wan_check())


def job_switch():
    state.update_switch(switch.collect())


def job_wifi():
    state.update_wifi(miwifi.collect())


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


def job_history():
    state.snapshot_history()


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

    _loop("ikuai", config.POLL_FAST, job_fast)
    _loop("devices", config.POLL_DEVICES, job_devices)
    _loop("pve", config.POLL_PVE, job_pve)
    _loop("probes", config.POLL_PING, job_probes)
    _loop("switch", config.POLL_SWITCH, job_switch)
    _loop("miwifi", config.POLL_MIWIFI, job_wifi)
    _loop("ha", config.POLL_HA, job_ha)
    _loop("wancheck", 30, job_wan)
    _loop("wanmerge", 30, _merge_wan_check)
    _loop("history", config.POLL_FAST, job_history)

    time.sleep(3)
    srv = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()

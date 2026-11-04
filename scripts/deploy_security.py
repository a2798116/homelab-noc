"""部署「来源分流」改造：内网可看面板，公网只能上报"""
import os
import shlex
import time

import paramiko

from _common import MONITOR_DIR, env, ssh_pass

LOCAL = MONITOR_DIR
PWD = ssh_pass()
HOST = env("FNLAB_HOST", required=True)
REMOTE = "/vol1/docker/monitor"
AUTH_USER = env("MONITOR_AUTH_USER", "admin")
AUTH_PASS = env("MONITOR_AUTH_PASS")


def conn(host):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, 22, env("FNLAB_USER", env("SSH_USER", "root")), PWD, timeout=20,
              look_for_keys=False, allow_agent=False)
    return c


def run(c, cmd, sudo=True, timeout=300, show=True):
    full = ("sudo -S -p '' bash -c %s" if sudo else "bash -c %s") % shlex.quote(cmd)
    si, so, se = c.exec_command(full, timeout=timeout)
    if sudo:
        si.write(PWD + "\n")
        si.flush()
    out = so.read().decode(errors="replace")
    err = se.read().decode(errors="replace")
    rc = so.channel.recv_exit_status()
    if show:
        if out.strip():
            print(out.rstrip()[:1800])
        if err.strip() and "chdir" not in err:
            print("[stderr]", err.rstrip()[:600])
    return rc, out, err


print("=" * 64)
print(">>> 上传文件")
c = conn(HOST)
run(c, "mkdir -p %s/app/static/agent" % REMOTE, show=False)
sftp = c.open_sftp()

# server.py 原样上传
sftp.put(os.path.join(LOCAL, "app", "server.py"), REMOTE + "/app/server.py")

# 采集端脚本去掉 CRLF，保证在 Linux 上能执行
for name in ("install.sh", "fnos-monitor-report.py"):
    src = os.path.join(LOCAL, "agent", name)
    with open(src, "r", encoding="utf-8") as f:
        text = f.read().replace("\r\n", "\n").replace("\r", "\n")
    with sftp.file(REMOTE + "/app/static/agent/" + name, "w") as f:
        f.write(text)

sftp.put(os.path.join(LOCAL, "docker-compose.yml"), REMOTE + "/docker-compose.yml")
sftp.close()
print("  server.py / static/agent/*.sh,*.py / docker-compose.yml 已上传")

run(c, "chmod 0755 %s/app/static/agent/*" % REMOTE, show=False)

print("=" * 64)
print(">>> 重建容器")
run(c, "cd %s && docker compose up -d --force-recreate 2>&1 | tail -6" % REMOTE)
time.sleep(5)
run(c, "docker ps --filter name=homelab-monitor --format '{{.Status}}'")

print("=" * 64)
print(">>> 容器内自检")
run(c, 'docker exec homelab-monitor sh -c "ls -l /app/static/agent/ && python3 -c \\"import ast; ast.parse(open(\'/app/server.py\').read()); print(\'syntax ok\')\\""')

print("=" * 64)
print(">>> 本机(内网)访问验证")
for label, url, extra in [
    ("内网 页面", "http://127.0.0.1:8930/", '-u %s:%s' % (AUTH_USER, AUTH_PASS)),
    ("内网 API", "http://127.0.0.1:8930/api/devices", '-u %s:%s' % (AUTH_USER, AUTH_PASS)),
    ("内网 健康", "http://127.0.0.1:8930/api/health", ""),
    ("内网 安装脚本", "http://127.0.0.1:8930/agent/install.sh", ""),
]:
    run(c, 'echo -n "%s: "; curl -s -m 8 %s -o /dev/null -w "%%{http_code}\\n" %s' % (label, extra, url))

print("=" * 64)
print(">>> 日志尾部")
run(c, "docker logs --tail 8 homelab-monitor 2>&1")
c.close()

print("=" * 64)
print(">>> 从公网侧验证（模拟外部来源）")
import base64
import json
import urllib.error
import urllib.request

BASE = env("PUBLIC_URL")
auth = base64.b64encode(("%s:%s" % (AUTH_USER, AUTH_PASS)).encode()).decode()


def probe(path, method="GET", body=None, headers=None):
    h = dict(headers or {})
    if "noauth" not in h.pop("_flags", []):
        h["Authorization"] = "Basic " + auth
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()[:200].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, "%s: %s" % (type(e).__name__, e)


cases = [
    ("公网 首页(应403)", "/", "GET", None, {}),
    ("公网 设备API(应403)", "/api/devices", "GET", None, {}),
    ("公网 安装脚本(应200)", "/agent/install.sh", "GET", None, {}),
    ("公网 采集脚本(应200)", "/agent/fnos-monitor-report.py", "GET", None, {}),
    ("公网 健康(应200)", "/api/health", "GET", None, {}),
]
for label, path, method, body, headers in cases:
    code, txt = probe(path, method, body, headers)
    print("  %-26s -> %s  %s" % (label, code, txt[:80].replace("\n", " ")))

# 用 FN7567 的令牌测试公网上报
payload = json.dumps({
    "ts": int(time.time()),
    "temps": {"cpu": 48.0, "nvme": 40.0, "pch": 40.0},
    "cpu": {"usage": 3.5, "load1": 0.1, "load5": 0.1, "load15": 0.1},
    "mem": {"used": 1024 ** 3, "total": 16 * 1024 ** 3},
    "uptime": 3600,
    "disks": [{"mount": "/vol1", "total": 429 * 1024 ** 3, "used": 4 * 1024 ** 3, "pct": 1}],
    "meta": {"name": "FN7567"},
}).encode()
code, txt = probe(
    "/api/report", "POST", payload,
    {"Content-Type": "application/json", "X-Device-Token": env("MONITOR_TOKEN_REMOTE")},
)
print("  %-26s -> %s  %s" % ("公网 上报(应200)", code, txt[:120]))

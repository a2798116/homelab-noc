"""启用 Basic Auth，并把公网地址切到 DDNS 域名"""
import os
import shlex
import paramiko

from _common import CENTER_URL, MONITOR_DIR, PUBLIC_URL, env, ssh_pass

LOCAL = MONITOR_DIR
PWD = ssh_pass()
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
            print(out.rstrip()[:1500])
        if err.strip() and "chdir" not in err:
            print("[stderr]", err.rstrip()[:600])
    return rc, out, err


# ---------- A. 中心节点：更新后端 + 配置，重建 ----------
print("=" * 64)
print(">>> A. FN-Lab 启用认证")
c = conn(env("FNLAB_HOST", required=True))
sftp = c.open_sftp()
sftp.put(os.path.join(LOCAL, "app", "server.py"), "/vol1/docker/monitor/app/server.py")
sftp.put(os.path.join(LOCAL, ".env"), "/vol1/docker/monitor/.env")
sftp.close()
print("  server.py / .env 已更新")
run(c, "cd /vol1/docker/monitor && docker compose up -d --force-recreate 2>&1 | tail -4")
run(c, "sleep 4; docker ps --filter name=homelab-monitor --format '{{.Status}}'")

print("--- 认证验证 ---")
run(c, 'echo -n "无凭据: "; curl -s -m 5 -o /dev/null -w "%{http_code}\\n" http://127.0.0.1:8930/api/devices')
run(c, 'echo -n "有凭据: "; curl -s -m 5 -u %s:%s -o /dev/null -w "%%{http_code}\\n" http://127.0.0.1:8930/api/devices' % (AUTH_USER, AUTH_PASS))
run(c, 'echo -n "健康检查(免认证): "; curl -s -m 5 -o /dev/null -w "%{http_code}\\n" http://127.0.0.1:8930/api/health')
c.close()

# ---------- B. 采集端：更新脚本与配置 ----------
print("=" * 64)
print(">>> B. 更新采集端")
targets = [
    (env("FNLAB_HOST"), "中心节点", "http://127.0.0.1:8930", env("MONITOR_TOKEN_CENTER"), ""),
    (env("FNNAS_HOST"), "远端节点", PUBLIC_URL, env("MONITOR_TOKEN_REMOTE"), ""),
]
for host, name, server, token, model in targets:
    print("--- %s (%s) -> %s" % (name, host, server))
    c = conn(host)
    sftp = c.open_sftp()
    sftp.put(os.path.join(LOCAL, "agent", "fnos-monitor-report.py"), "/tmp/agent.py")
    env = "SERVER=%s\nTOKEN=%s\nDEVICE_NAME=%s\nDEVICE_MODEL=%s\nAUTH_USER=%s\nAUTH_PASS=%s\n" % (
        server, token, name, model, AUTH_USER, AUTH_PASS)
    with sftp.file("/tmp/monitor.env", "w") as f:
        f.write(env)
    sftp.close()
    run(c, "install -m 0755 -o root -g root /tmp/agent.py /usr/local/bin/fnos-monitor-report.py && "
          "install -m 0600 -o root -g root /tmp/monitor.env /etc/fnos-monitor.env && "
          "rm -f /tmp/agent.py /tmp/monitor.env", show=False)
    run(c, "/usr/bin/python3 /usr/local/bin/fnos-monitor-report.py 2>&1")
    c.close()

# ---------- C. 汇总 ----------
print("=" * 64)
print(">>> C. 结果")
import base64
import json
import urllib.request

auth = base64.b64encode(("%s:%s" % (AUTH_USER, AUTH_PASS)).encode()).decode()
for label, url in [("内网", CENTER_URL), ("公网", PUBLIC_URL)]:
    if not url:
        continue
    req = urllib.request.Request(url + "/api/devices", headers={"Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.load(r)
    print("[%s] %s -> %d 台设备" % (label, url, len(d["devices"])))
    for dev in d["devices"]:
        L = dev.get("latest")
        print("   %-8s 在线=%-5s 温度=%s" % (dev["name"], dev["online"], L["cpu_temp"] if L else None))

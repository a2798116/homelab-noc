import os
import shlex
import paramiko

from _common import CENTER_URL, MONITOR_DIR, PUBLIC_URL, env, ssh_pass

LOCAL = MONITOR_DIR
PWD = ssh_pass()


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
            print(out.rstrip()[:1200])
        if err.strip() and "chdir" not in err:
            print("[stderr]", err.rstrip()[:600])
    return rc, out, err


# ---------- A. 中心服务：更新前端 + 重挂载 ----------
print("=" * 64)
print(">>> A. FN-Lab 更新前端")
c = conn(env("FNLAB_HOST", required=True))
sftp = c.open_sftp()
sftp.put(os.path.join(LOCAL, "app", "static", "index.html"),
         "/vol1/docker/monitor/app/static/index.html")
sftp.put(os.path.join(LOCAL, "docker-compose.yml"),
         "/vol1/docker/monitor/docker-compose.yml")
sftp.close()
print("  文件已更新")
run(c, "cd /vol1/docker/monitor && docker compose up -d --force-recreate 2>&1 | tail -6")
run(c, "sleep 3; docker ps --filter name=homelab-monitor --format '{{.Status}}'")
run(c, 'curl -s -m 5 -o /dev/null -w "内网 HTTP %{http_code} size=%{size_download}\\n" http://127.0.0.1:8930/')
c.close()

# ---------- B. FN7567：上报地址切到公网 ----------
print("=" * 64)
print(">>> B. FN7567 切换上报地址")
c = conn(env("FNNAS_HOST", required=True))
run(c, "sed -i 's|^SERVER=.*|SERVER=" + PUBLIC_URL + "|' /etc/fnos-monitor.env && cat /etc/fnos-monitor.env")
print("--- 试跑 ---")
run(c, "/usr/bin/python3 /usr/local/bin/fnos-monitor-report.py 2>&1")
c.close()

# ---------- C. 汇总 ----------
print("=" * 64)
print(">>> C. 数据汇总")
import urllib.request
import json
with urllib.request.urlopen(CENTER_URL + "/api/devices", timeout=10) as r:
    d = json.load(r)
for dev in d["devices"]:
    L = dev.get("latest")
    print("设备:", dev["name"], "| 在线:", dev["online"])
    if L:
        print("   CPU 温度:", L["cpu_temp"], "| NVMe:", L["nvme_temp"], "| 负载:", L["load1"],
              "| 内存: %.0f%%" % (L["mem_used"] / L["mem_total"] * 100))
        print("   磁盘:", [(x["mount"], str(x["pct"]) + "%") for x in L["disks"]])
    else:
        print("   无数据")

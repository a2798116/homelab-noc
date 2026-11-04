"""把本地 monitor/agent 下的采集端推送到中心（10.32）并重启容器"""
import shlex

import paramiko

from _common import MONITOR_DIR, env, ssh_pass

HOST = env("FNLAB_HOST", required=True)
USER = env("FNLAB_USER", env("SSH_USER", "root"))
PWD = ssh_pass()
LOCAL = MONITOR_DIR
REMOTE = "/vol1/docker/monitor"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, 22, USER, PWD, timeout=20, look_for_keys=False, allow_agent=False)


def run(cmd, timeout=300):
    full = "sudo -S -p '' bash -c %s" % shlex.quote(cmd)
    si, so, se = c.exec_command(full, timeout=timeout)
    si.write(PWD + "\n")
    si.flush()
    out = so.read().decode(errors="replace")
    err = se.read().decode(errors="replace")
    rc = so.channel.recv_exit_status()
    print("$", cmd[:120])
    if out.strip():
        print(out.rstrip()[:2000])
    if err.strip() and "password" not in err.lower():
        print("[stderr]", err.rstrip()[:800])
    print("  rc =", rc)
    return rc


sftp = c.open_sftp()
pairs = [
    (LOCAL + r"\agent\fnos-monitor-report.py", REMOTE + "/app/static/agent/fnos-monitor-report.py"),
    (LOCAL + r"\agent\install.sh", REMOTE + "/app/static/agent/install.sh"),
    (LOCAL + r"\app\static\index.html", REMOTE + "/app/static/index.html"),
]
for lp, rp in pairs:
    sftp.put(lp, rp)
    print("put ->", rp)
sftp.close()

run("chmod 644 %s/app/static/agent/*" % REMOTE)
run("md5sum %s/app/static/agent/*" % REMOTE)
run("cd %s && docker compose up -d --no-build && docker compose ps" % REMOTE, timeout=300)
c.close()

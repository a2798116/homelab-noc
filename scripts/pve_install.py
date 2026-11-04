"""PVE 宿主机接入 Homelab Monitor：预演 -> 安装 -> 验证"""
import json

import paramiko

from _common import AGENT_PY, env, ssh_pass

HOST = env("PVE_HOST", required=True)
USER = env("PVE_USER", "root")
PWD = ssh_pass()
LOCAL = AGENT_PY
CENTER = env("CENTER_URL", required=True)
TOKEN = env("MONITOR_DEVICE_TOKEN")
NAME = env("MONITOR_DEVICE_NAME", "PVE-宿主")
MODEL = env("MONITOR_DEVICE_MODEL", "")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, 22, USER, PWD, timeout=20, look_for_keys=False, allow_agent=False)


def sh(cmd, timeout=180, show=True):
    _, so, se = c.exec_command(cmd, timeout=timeout)
    out = so.read().decode("utf-8", "replace")
    err = se.read().decode("utf-8", "replace")
    rc = so.channel.recv_exit_status()
    if show:
        print("$", cmd[:140])
        if out.strip():
            print(out.rstrip()[:4000])
        if err.strip():
            print("[stderr]", err.rstrip()[:1200])
        print("  rc =", rc)
    return rc, out, err


print("=" * 62, "\n[1] 预演：把采集脚本放 /tmp 后 dry-run")
sftp = c.open_sftp()
sftp.put(LOCAL, "/tmp/fnos-monitor-report.py")
sftp.close()
rc, out, err = sh("python3 /tmp/fnos-monitor-report.py --dry-run 2>&1 | head -80")

print("=" * 62, "\n[2] 正式安装（走中心一键接入，MOUNTS=/）")
cmd = (
    'curl -fsSL %s/agent/install.sh | SERVER=%s TOKEN=%s NAME="%s" MODEL="%s" MOUNTS=/ INTERVAL=5min sh'
    % (CENTER, CENTER, TOKEN, NAME, MODEL)
)
sh(cmd, timeout=300)

print("=" * 62, "\n[3] 验证")
sh("cat /etc/fnos-monitor.env; echo ---; systemctl is-enabled fnos-monitor-report.timer; systemctl is-active fnos-monitor-report.timer")
sh("journalctl -u fnos-monitor-report.service -n 12 --no-pager -o cat 2>&1 | tail -12")
c.close()

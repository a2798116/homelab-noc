"""在两台 fnOS 设备上安装采集端（脚本 + env + systemd timer）"""
import shlex
import sys
import paramiko

from _common import AGENT_PY, env, ssh_pass

LOCAL_AGENT = AGENT_PY
PWD = ssh_pass()

SERVICE = """[Unit]
Description=Homelab Monitor 状态上报
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/python3 /usr/local/bin/fnos-monitor-report.py
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=120
"""

TIMER = """[Unit]
Description=每 5 分钟上报一次设备状态

[Timer]
OnBootSec=90s
OnUnitActiveSec=5min
AccuracySec=20s
Persistent=true
Unit=fnos-monitor-report.service

[Install]
WantedBy=timers.target
"""

# 两台采集端：地址、设备令牌全部来自 .env
TARGETS = [
    dict(host=env("FNLAB_HOST"), name="中心节点", server="http://127.0.0.1:8930",
         token=env("MONITOR_TOKEN_CENTER"), model=env("MONITOR_DEVICE_MODEL", "")),
    dict(host=env("FNNAS_HOST"), name="远端节点", server=env("CENTER_URL"),
         token=env("MONITOR_TOKEN_REMOTE"), model=""),
]


def deploy(t):
    print("=" * 64)
    print(">>> %s (%s)" % (t["name"], t["host"]))
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(t["host"], 22, env("SSH_USER", "root"), PWD, timeout=20,
              look_for_keys=False, allow_agent=False)

    def run(cmd, sudo=True, timeout=180, show=True):
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

    rc, out, _ = run("command -v /usr/bin/python3 && /usr/bin/python3 -V", sudo=False, show=True)
    if "/usr/bin/python3" not in out:
        print("!! 缺少 python3，跳过")
        c.close()
        return False

    # 上传脚本
    sftp = c.open_sftp()
    sftp.put(LOCAL_AGENT, "/tmp/fnos-monitor-report.py")
    sftp.close()
    run("install -m 0755 -o root -g root /tmp/fnos-monitor-report.py /usr/local/bin/fnos-monitor-report.py")
    run("rm -f /tmp/fnos-monitor-report.py", show=False)

    # 配置文件
    env = "SERVER=%s\nTOKEN=%s\nDEVICE_NAME=%s\nDEVICE_MODEL=%s\n" % (
        t["server"], t["token"], t["name"], t["model"])
    sftp = c.open_sftp()
    with sftp.file("/tmp/fnos-monitor.env", "w") as f:
        f.write(env)
    sftp.close()
    run("install -m 0600 -o root -g root /tmp/fnos-monitor.env /etc/fnos-monitor.env")
    run("rm -f /tmp/fnos-monitor.env", show=False)

    # systemd 单元
    sftp = c.open_sftp()
    with sftp.file("/tmp/fnos-monitor-report.service", "w") as f:
        f.write(SERVICE)
    with sftp.file("/tmp/fnos-monitor-report.timer", "w") as f:
        f.write(TIMER)
    sftp.close()
    run("install -m 0644 /tmp/fnos-monitor-report.service /etc/systemd/system/ && "
        "install -m 0644 /tmp/fnos-monitor-report.timer /etc/systemd/system/ && "
        "rm -f /tmp/fnos-monitor-report.*", show=False)
    run("systemctl daemon-reload && systemctl enable --now fnos-monitor-report.timer 2>&1")

    # 首次上报验证
    print("--- 试跑一次 ---")
    run("/usr/bin/python3 /usr/local/bin/fnos-monitor-report.py 2>&1")
    print("--- timer 状态 ---")
    run("systemctl list-timers fnos-monitor-report.timer --no-pager 2>&1 | head -4")

    c.close()
    return True


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for t in TARGETS:
        if only and only not in (t["name"], t["host"]):
            continue
        try:
            deploy(t)
        except Exception as exc:  # noqa: BLE001
            print("!! %s 失败: %s: %s" % (t["name"], type(exc).__name__, exc))

"""勘察 PVE 宿主机环境，为接入监控做准备"""
import paramiko

from _common import CENTER_URL, env, ssh_pass

HOST = env("PVE_HOST", required=True)
USER = env("PVE_USER", "root")
PWD = ssh_pass()

CMDS = [
    ("主机名/版本", "hostname; pveversion 2>/dev/null | head -3; cat /etc/os-release | grep PRETTY"),
    ("内核", "uname -r"),
    ("python3", "command -v python3 && python3 -V"),
    ("curl", "command -v curl; command -v wget"),
    ("systemd", "pidof systemd >/dev/null && echo systemd-ok"),
    ("挂载点", "df -hT -x tmpfs -x devtmpfs -x overlay | head -20"),
    ("CPU/内存", "nproc; free -g | head -2"),
    ("到中心连通", "curl -s -m 5 -o /dev/null -w 'http_code=%{http_code}\\n' "
                   + CENTER_URL + "/api/health; curl -s -m 5 " + CENTER_URL + "/api/health"),
    ("hwmon", "ls /sys/class/hwmon/ | head; for d in /sys/class/hwmon/hwmon*; do echo -n \"$d \"; cat $d/name 2>/dev/null; done"),
    ("已有监控", "ls -l /etc/fnos-monitor.env 2>/dev/null; systemctl is-active fnos-monitor-report.timer 2>/dev/null"),
]

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, 22, USER, PWD, timeout=15, look_for_keys=False, allow_agent=False)
for title, cmd in CMDS:
    _, out, err = c.exec_command(cmd, timeout=40)
    o = out.read().decode("utf-8", "replace").rstrip()
    e = err.read().decode("utf-8", "replace").rstrip()
    print("=" * 60)
    print("##", title)
    print(o or "(空)")
    if e:
        print("[stderr]", e[:300])
c.close()

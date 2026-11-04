import shlex
import paramiko

from _common import env, ssh_pass

HOST = env("FNNAS_HOST", required=True)
PORT = 22
USER = env("FNNAS_USER", env("SSH_USER", "root"))
PWD = ssh_pass()

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, PORT, USER, PWD, timeout=15, look_for_keys=False, allow_agent=False)


def run(cmd, sudo=False, timeout=60):
    if sudo:
        full = "sudo -S -p '' bash -c %s" % shlex.quote(cmd)
    else:
        full = "bash -c %s" % shlex.quote(cmd)
    si, so, se = c.exec_command(full, timeout=timeout)
    if sudo:
        si.write(PWD + "\n")
        si.flush()
    out = so.read().decode(errors="replace")
    err = se.read().decode(errors="replace")
    rc = so.channel.recv_exit_status()
    return rc, out, err


print("=" * 60)
print("[1] 身份")
for cmd, sudo in [("id", False), ("id", True)]:
    rc, out, err = run(cmd, sudo)
    print(("sudo " if sudo else "") + "id ->", rc, out.strip() or err.strip())

print("=" * 60)
print("[2] 当前 logind 配置")
print(run("grep -vE '^\\s*(#|$)' /etc/systemd/logind.conf", True)[1] or "(主配置无生效项)")

print("[3] drop-in 目录")
print(run("ls -la /etc/systemd/logind.conf.d/ 2>/dev/null || echo NO_DROPDIR", True)[1])

print("[4] 有效值")
for k in ["HandleLidSwitch", "HandleLidSwitchExternalPower", "HandleLidSwitchDocked", "LidSwitchIgnoreInhibited", "IdleAction"]:
    rc, out, err = run("systemctl show systemd-logind -p %s" % k, False)
    print(" ", out.strip())

print("=" * 60)
print("[5] 睡眠能力与策略")
print("mem_sleep:", run("cat /sys/power/mem_sleep", True)[1].strip())
print("state:", run("cat /sys/power/state", True)[1].strip())
print("masked targets:")
print(run("systemctl list-unit-files 'sleep.target' 'suspend.target' 'hibernate.target' --no-pager 2>/dev/null", True)[1])

print("=" * 60)
print("[6] 是否有 acpid / 其他合盖处理")
print(run("ls /etc/acpi/events/ 2>/dev/null; systemctl is-active acpid 2>/dev/null; systemctl is-active systemd-logind", True)[1])

print("=" * 60)
print("[7] 系统")
print(run("hostnamectl 2>/dev/null | head -12", True)[1])

c.close()

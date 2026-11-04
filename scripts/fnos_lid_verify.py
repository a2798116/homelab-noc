import shlex
import paramiko

from _common import env, ssh_pass

HOST = env("FNNAS_HOST", required=True)
PORT = 22
USER = env("FNNAS_USER", env("SSH_USER", "root"))
PWD = ssh_pass()

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, PORT, USER, PWD, timeout=20, look_for_keys=False, allow_agent=False)


def run(cmd, sudo=True, timeout=60):
    full = ("sudo -S -p '' bash -c %s" if sudo else "bash -c %s") % shlex.quote(cmd)
    si, so, se = c.exec_command(full, timeout=timeout)
    if sudo:
        si.write(PWD + "\n")
        si.flush()
    out = so.read().decode(errors="replace")
    err = se.read().decode(errors="replace")
    rc = so.channel.recv_exit_status()
    return rc, out, err


print("=" * 60)
print("[1] logind 实际生效值")
rc, out, err = run("systemctl show systemd-logind --property=HandleLidSwitch --property=HandleLidSwitchExternalPower --property=HandleLidSwitchDocked --property=LidSwitchIgnoreInhibited")
print(out.strip() or "(空)")

print("=" * 60)
print("[2] 合并后的完整配置（cat-config）")
rc, out, err = run("systemd-analyze cat-config systemd/logind.conf 2>/dev/null | grep -vE '^\\s*(#|$)'")
print(out.strip() or "(无)")

print("=" * 60)
print("[3] 服务状态")
rc, out, err = run("systemctl status systemd-logind --no-pager -n 0 2>&1 | head -6")
print(out.strip())

print("=" * 60)
print("[4] 盖子开关设备")
rc, out, err = run("cat /proc/acpi/button/lid/*/state 2>/dev/null || echo NO_LID_DEVICE")
print(out.strip())

print("=" * 60)
print("[5] 当前是否处于睡眠/挂起状态")
rc, out, err = run("cat /sys/power/state; echo '---'; systemctl is-system-running; echo '---'; who")
print(out.strip())

c.close()

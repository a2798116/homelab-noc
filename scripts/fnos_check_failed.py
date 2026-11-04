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


print("[1] 失败单元")
rc, out, err = run("systemctl --failed --no-pager --plain 2>&1 | head -20")
print(out.strip() or "(无)")

print("=" * 60)
print("[2] busctl 读取 logind 的 HandleLidSwitch")
rc, out, err = run("busctl get-property org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager HandleLidSwitch 2>&1")
print(out.strip() or err.strip() or "(空)")

print("=" * 60)
print("[3] 各失败单元原因（若有）")
rc, out, _ = run("systemctl --failed --no-pager --plain 2>/dev/null | awk 'NR>1 && NF>1 {print $1}'")
units = [u for u in out.split() if u.endswith((".service", ".target", ".mount", ".socket", ".timer"))]
if not units:
    print(" 无失败单元")
for u in units[:8]:
    _, o, _ = run("systemctl status %s --no-pager -n 4 2>&1 | head -10" % u)
    print("---", u)
    print(o.rstrip())

print("=" * 60)
print("[4] 近期是否有 suspend 记录")
rc, out, err = run("journalctl -b -u systemd-logind --no-pager -n 15 2>&1 | tail -15")
print(out.strip())

c.close()

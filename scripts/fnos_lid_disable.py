import shlex
import paramiko

from _common import env, ssh_pass

HOST = env("FNNAS_HOST", required=True)
PORT = 22
USER = env("FNNAS_USER", env("SSH_USER", "root"))
PWD = ssh_pass()

CONF = """# 合盖不休眠：屏幕关闭但系统继续运行（NAS 场景）
# 由 fnos-cli 维护 2026-10-30
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
LidSwitchIgnoreInhibited=yes
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, PORT, USER, PWD, timeout=15, look_for_keys=False, allow_agent=False)


def run(cmd, sudo=False, timeout=60, quiet=False):
    full = ("sudo -S -p '' bash -c %s" if sudo else "bash -c %s") % shlex.quote(cmd)
    si, so, se = c.exec_command(full, timeout=timeout)
    if sudo:
        si.write(PWD + "\n")
        si.flush()
    out = so.read().decode(errors="replace")
    err = se.read().decode(errors="replace")
    rc = so.channel.recv_exit_status()
    if not quiet:
        print("$", cmd[:90])
        if out.strip():
            print(out.rstrip())
        if err.strip():
            print("[stderr]", err.rstrip())
        print("  rc =", rc)
    return rc, out, err


print("=" * 60)
print("[1] 写入配置")
run("mkdir -p /etc/systemd/logind.conf.d", sudo=True, quiet=True)

sftp = c.open_sftp()
with sftp.file("/tmp/99-lid-ignore.conf", "w") as f:
    f.write(CONF)
sftp.close()
print("  已上传 /tmp/99-lid-ignore.conf")

run("install -m 0644 -o root -g root /tmp/99-lid-ignore.conf /etc/systemd/logind.conf.d/99-lid-ignore.conf", sudo=True)
run("rm -f /tmp/99-lid-ignore.conf", sudo=True, quiet=True)

print("=" * 60)
print("[2] 校验文件")
run("ls -l /etc/systemd/logind.conf.d/99-lid-ignore.conf && cat /etc/systemd/logind.conf.d/99-lid-ignore.conf", sudo=True)

print("=" * 60)
print("[3] 重启 logind（后台延迟，避免断开当前连接）")
run("nohup sh -c 'sleep 1; systemctl restart systemd-logind' >/dev/null 2>&1 & echo dispatched", sudo=True)

c.close()
print("done")

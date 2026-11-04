import os
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


def run(cmd, sudo=True, timeout=600, show=True):
    full = ("sudo -S -p '' bash -c %s" if sudo else "bash -c %s") % shlex.quote(cmd)
    si, so, se = c.exec_command(full, timeout=timeout)
    if sudo:
        si.write(PWD + "\n")
        si.flush()
    out = so.read().decode(errors="replace")
    err = se.read().decode(errors="replace")
    rc = so.channel.recv_exit_status()
    if show:
        print("$", cmd[:110])
        if out.strip():
            print(out.rstrip()[:3000])
        if err.strip():
            print("[stderr]", err.rstrip()[:1500])
        print("  rc =", rc)
    return rc, out, err


print("=" * 60)
print("[1] 准备目录")
run("mkdir -p %s/app/static %s/data" % (REMOTE, REMOTE))
run("chown -R %s:%s %s" % (USER, USER, REMOTE))
run("ls -ld %s" % REMOTE)

print("=" * 60)
print("[2] 上传文件")
sftp = c.open_sftp()
files = [
    (r"app\server.py", "app/server.py"),
    (os.path.join("app", "static", "index.html"), "app/static/index.html"),
    ("Dockerfile", "Dockerfile"),
    ("docker-compose.yml", "docker-compose.yml"),
    (".env", ".env"),
]
for local_rel, remote_rel in files:
    lp = os.path.join(LOCAL, local_rel)
    rp = REMOTE + "/" + remote_rel
    sftp.put(lp, rp)
    print("  ->", remote_rel, os.path.getsize(lp), "bytes")
sftp.close()

print("=" * 60)
print("[3] 校验远端 .env")
run("cat %s/.env" % REMOTE)

print("=" * 60)
print("[4] 构建并启动")
rc, out, err = run("cd %s && docker compose up -d --build 2>&1 | tail -25" % REMOTE, timeout=900)

print("=" * 60)
print("[5] 容器状态")
run("docker ps --filter name=homelab-monitor --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'")
run("cd %s && docker compose logs --tail 30 2>&1" % REMOTE)

c.close()
print("done")

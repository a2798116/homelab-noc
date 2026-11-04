#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 server.py 与 .env 推到 FN-Lab，重建容器，验证内网免密 + 公网仍 403"""
import hashlib
import os

import paramiko

from _common import MONITOR_DIR, env, ssh_pass

HOST = env("FNLAB_HOST", required=True)
PWD = ssh_pass()
LOCAL = os.path.join(MONITOR_DIR, "app", "server.py")
REMOTE = "/vol1/docker/monitor/app/server.py"
ENV_LOCAL = os.path.join(MONITOR_DIR, ".env")
ENV_REMOTE = "/vol1/docker/monitor/.env"


def sh(c, cmd, t=180):
    import shlex

    full = "sudo -S -p '' bash -c %s" % shlex.quote(cmd)
    si, so, se = c.exec_command(full, timeout=t)
    si.write(PWD + "\n")
    si.flush()
    out = so.read().decode(errors="replace").strip()
    err = se.read().decode(errors="replace").strip()
    return out or err


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, env("FNLAB_USER", env("SSH_USER", "root")), PWD, timeout=20,
              look_for_keys=False, allow_agent=False)

    print(">>> 1. 上传 server.py 与 .env")
    sftp = c.open_sftp()
    sftp.put(LOCAL, "/tmp/server.py.new")
    with open(ENV_LOCAL, "r", encoding="utf-8") as f:
        env_text = f.read().replace("\r\n", "\n").replace("\r", "\n")
    with sftp.file("/tmp/env.new", "w") as f:
        f.write(env_text)
    sftp.close()
    print("  server.py md5:", hashlib.md5(open(LOCAL, "rb").read()).hexdigest())

    print()
    print(">>> 2. 备份并替换")
    print(sh(c, "cp {r} {r}.bak.$(date +%s) && install -m 644 -o root -g root /tmp/server.py.new {r} && rm -f /tmp/server.py.new && md5sum {r}".format(r=REMOTE)))
    print(sh(c, "cp {e} {e}.bak.$(date +%s) && install -m 600 -o root -g root /tmp/env.new {e} && rm -f /tmp/env.new && grep -c . {e}".format(e=ENV_REMOTE)))

    print()
    print(">>> 3. 语法检查")
    print(sh(c, "docker exec homelab-monitor python3 -c \"import ast; ast.parse(open('/app/server.py').read()); print('syntax ok')\""))

    print()
    print(">>> 4. 重建容器（重读 .env）")
    print(sh(c, "cd /vol1/docker/monitor && docker compose up -d --no-build 2>&1 | tail -4; sleep 4; docker ps --filter name=homelab-monitor --format '{{.Status}}'"))

    print()
    print(">>> 5. 容器内自检")
    print(sh(c, "docker exec homelab-monitor python3 -c \"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8930/api/health',timeout=5).read().decode())\""))

    print()
    print(">>> 6. 环境变量确认")
    print(sh(c, "docker exec homelab-monitor printenv UNTRUSTED_SRC; docker exec homelab-monitor printenv TRUSTED_NETS"))

    c.close()
    print("完成")


if __name__ == "__main__":
    main()

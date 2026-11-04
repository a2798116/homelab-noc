# -*- coding: utf-8 -*-
"""增量部署 NOC 到 fnOS 中心节点

行为：
  1. 把 noc/ 下的后端与前端逐个传上去
  2. 把本地 noc/.env（gitignored）传到远端，并把 HA_TOKEN_FILE 改成容器内路径
  3. 把本地 noc/config.local.json（gitignored）传到远端的 data/ 目录
  4. 重建容器并打印状态

依赖环境变量（见根目录 .env.example）：
  NOC_DEPLOY_HOST / SSH_USER / SSH_PASSWORD
"""
import os
import posixpath
import sys
import time

import paramiko

from _common import NOC_DIR, env, ssh_pass

HOST = env("NOC_DEPLOY_HOST", env("FNLAB_HOST"), required=True)
USER = env("NOC_DEPLOY_USER", env("SSH_USER", "root"))
PW = ssh_pass()
REMOTE = env("NOC_REMOTE_DIR", "/vol1/docker/noc")

FILES = [
    "app/config.py",
    "app/state.py",
    "app/server.py",
    "app/assets.py",
    "app/collectors/__init__.py",
    "app/collectors/ikuai.py",
    "app/collectors/pve.py",
    "app/collectors/localnet.py",
    "app/collectors/pinger.py",
    "app/collectors/switch.py",
    "app/collectors/miwifi.py",
    "app/collectors/ha.py",
    "app/static/index.html",
    "app/static/style.css",
    "app/static/app.js",
    "docker-compose.yml",
    "Dockerfile",
    "README.md",
]


def sh(c, cmd, sudo=True, quiet=False):
    full = "echo '%s' | sudo -S bash -lc %r" % (PW, cmd) if sudo else cmd
    _, o, e = c.exec_command(full)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    if not quiet and (out.strip() or err.strip()):
        print((out + err).strip()[:1500])
    return out


def _env_text():
    """读本地 noc/.env，把容器内路径改对"""
    p = os.path.join(NOC_DIR, ".env")
    if not os.path.isfile(p):
        sys.exit("缺少 %s —— 先复制 noc/.env.example 为 noc/.env 并填写" % p)
    lines = []
    for raw in open(p, "r", encoding="utf-8"):
        line = raw.rstrip("\n")
        if line.strip().startswith("HA_TOKEN_FILE="):
            line = "HA_TOKEN_FILE=/data/ha_token.json"
        lines.append(line)
    return "\n".join(lines) + "\n"


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, username=USER, password=PW, timeout=15,
              allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()

    print("== 1. 上传代码 ==")
    for rel in FILES:
        lp = os.path.join(NOC_DIR, rel)
        if not os.path.isfile(lp):
            print("  跳过（不存在）:", rel)
            continue
        tmp = "/tmp/noc_stage_" + os.path.basename(rel)
        sftp.put(lp, tmp)
        dst = posixpath.join(REMOTE, rel)
        sh(c, "mkdir -p %s && cp %s %s && chmod 644 %s"
           % (posixpath.dirname(dst), tmp, dst, dst), quiet=True)
        print("  ✓", rel, "%.1fKB" % (os.path.getsize(lp) / 1024))

    print("\n== 2. 本地站点配置 ==")
    cfg = os.path.join(NOC_DIR, "config.local.json")
    if os.path.isfile(cfg):
        sh(c, "mkdir -p %s/data && chmod 777 %s/data" % (REMOTE, REMOTE), quiet=True)
        sftp.put(cfg, "/tmp/noc_local.json")
        sh(c, "cp /tmp/noc_local.json %s/data/config.local.json && chmod 600 %s/data/config.local.json"
           % (REMOTE, REMOTE), quiet=True)
        print("  ✓ config.local.json → data/")
    else:
        print("  跳过（没有 config.local.json，面板将使用通用标签）")

    print("\n== 3. 写入 .env ==")
    with sftp.open("/tmp/noc_env", "w") as f:
        f.write(_env_text())
    sh(c, "cp /tmp/noc_env %s/.env && chmod 600 %s/.env" % (REMOTE, REMOTE), quiet=True)
    print("  ✓ .env")

    print("\n== 4. 重建容器 ==")
    sh(c, "cd %s && docker compose up -d --force-recreate 2>&1 | tail -12" % REMOTE)

    sftp.close()
    time.sleep(8)

    print("\n== 5. 状态 ==")
    sh(c, "docker ps --filter name=homelab-noc --format '{{.Names}}  {{.Status}}  {{.Ports}}'")
    sh(c, "docker logs --tail 25 homelab-noc 2>&1")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

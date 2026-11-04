# -*- coding: utf-8 -*-
"""往 fnOS 中心节点传单个文件（不重建容器）

NOC 的 app/ 是只读挂载进容器的，改前端只要把文件传上去、刷新浏览器即可，
不必跑完整的 deploy_noc.py（那个会 force-recreate 容器，慢且会打断 SSE）。

用法：
    python scripts/put_file.py noc/app/static/index.html
    python scripts/put_file.py noc/app/static/style.css
    python scripts/put_file.py noc/app/state.py --remote /vol1/docker/noc/app/state.py

默认远端路径 = <NOC_REMOTE_DIR>/<参数里的相对路径>，所以传 noc/ 下的文件
直接给相对路径就行，不要去数 /vol1/docker/noc 前缀。

依赖环境变量（见根目录 .env.example）：
  NOC_DEPLOY_HOST / SSH_USER / SSH_PASSWORD
"""
import argparse
import os
import posixpath
import sys

import paramiko

from _common import NOC_DIR, ROOT, env, ssh_pass

HOST = env("NOC_DEPLOY_HOST", env("FNLAB_HOST"), required=True)
USER = env("NOC_DEPLOY_USER", env("SSH_USER", "root"))
PW = ssh_pass()
REMOTE = env("NOC_REMOTE_DIR", "/vol1/docker/noc")


def main():
    ap = argparse.ArgumentParser(description="传单个文件到 fnOS（不重建容器）")
    ap.add_argument("local", help="本地文件路径")
    ap.add_argument("--remote", help="远端绝对路径（默认按 noc/ 相对路径推导）")
    ap.add_argument("--mode", default="644", help="远端权限（默认 644）")
    args = ap.parse_args()

    lp = os.path.abspath(args.local)
    if not os.path.isfile(lp):
        sys.exit("本地文件不存在: %s" % lp)

    if args.remote:
        dst = args.remote
    else:
        # 把路径归一到 noc/ 之后的部分
        rel = None
        for base in (NOC_DIR, ROOT):
            try:
                rel = os.path.relpath(lp, base).replace(os.sep, "/")
                break
            except ValueError:
                continue
        if rel is None or rel.startswith(".."):
            sys.exit("文件不在项目目录内，请用 --remote 指定远端路径")
        if rel.startswith("noc/"):
            rel = rel[4:]
        dst = posixpath.join(REMOTE, rel)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, username=USER, password=PW, timeout=15,
              allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    tmp = "/tmp/put_" + os.path.basename(lp)
    sftp.put(lp, tmp)
    sftp.close()

    cmd = "mkdir -p %s && cp %s %s && chmod %s %s" % (
        posixpath.dirname(dst), tmp, dst, args.mode, dst)
    _, o, e = c.exec_command("echo '%s' | sudo -S bash -lc %r" % (PW, cmd))
    out = (o.read() + e.read()).decode("utf-8", "replace").strip()
    c.close()

    if out and "sudo" not in out.lower():
        print(out[:400])

    print("✓ %s → %s  (%.1f KB)" % (
        os.path.relpath(lp, ROOT).replace(os.sep, "/"), dst,
        os.path.getsize(lp) / 1024))
    if dst.endswith((".html", ".css", ".js")):
        print("  前端文件：刷新浏览器即生效，无需重启容器")
    else:
        print("  后端文件：需要 docker compose restart 才生效")


if __name__ == "__main__":
    main()

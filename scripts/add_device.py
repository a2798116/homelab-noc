"""在中心登记一台新设备，并打印它的接入命令

用法：
    python scripts/add_device.py <显示名> [设备ID] [型号描述]
    python scripts/add_device.py 客厅小主机 living "N100 / 16G"

设备ID 默认由显示名生成（小写、保留字母数字与连字符）。
"""
import base64
import json
import re
import secrets
import sys
import urllib.error
import urllib.request

from _common import env

CENTER = env("CENTER_URL", required=True)
PUBLIC = env("PUBLIC_URL")
AUTH_USER = env("MONITOR_AUTH_USER", "admin")
AUTH_PASS = env("MONITOR_AUTH_PASS")
ADMIN_TOKEN = env("MONITOR_ADMIN_TOKEN")


def api(path, payload=None):
    headers = {
        "Authorization": "Basic " + base64.b64encode(("%s:%s" % (AUTH_USER, AUTH_PASS)).encode()).decode(),
        "Content-Type": "application/json",
    }
    if payload is not None:
        headers["X-Admin-Token"] = ADMIN_TOKEN
    req = urllib.request.Request(
        CENTER + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method="POST" if payload is not None else "GET",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    name = sys.argv[1]
    did = sys.argv[2] if len(sys.argv) > 2 else None
    model = sys.argv[3] if len(sys.argv) > 3 else ""
    if not did:
        did = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "dev-" + secrets.token_hex(3)

    token = secrets.token_hex(16)
    try:
        api("/api/devices", {"id": did, "name": name, "token": token, "meta": {"model": model}})
    except urllib.error.HTTPError as e:
        print("[失败] HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        return 1

    print("已登记设备")
    print("  显示名 : %s" % name)
    print("  设备ID : %s" % did)
    print("  令牌   : %s" % token)
    print()
    print("=" * 70)
    print("在该设备上以 root 执行下面这一条（跨网段/公网用第一条，同内网用第二条）：")
    print()
    print("curl -fsSL %s/agent/install.sh | \\" % PUBLIC)
    print('  sudo SERVER=%s TOKEN=%s NAME=%s MODEL="%s" sh' % (PUBLIC, token, name, model or "（型号）"))
    print()
    print("curl -fsSL %s/agent/install.sh | \\" % CENTER)
    print('  sudo SERVER=%s TOKEN=%s NAME=%s MODEL="%s" sh' % (CENTER, token, name, model or "（型号）"))
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())

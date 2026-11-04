"""在 WireGuard 服务端（LXC）重建 wg0

拓扑：
  手机          ─┐
                 ├─ 公网端点:端口/udp → 服务端 wg0 ─→ 内网网段
  远端 NAS      ─┘                              └─→ 对端 NAS 单机路由

所有地址、网段、端点都从 .env 读取（模板见 .env.example）：
  WG_HOST / WG_USER / SSH_PASSWORD / WG_ENDPOINT / WG_PORT
  WG_NET / WG_SRV_IP / WG_PHONE_IP / WG_NAS_IP / WG_LAN / WG_NAS_LAN
"""
import os
import shutil
import time

import paramiko

from _common import ROOT, env, ssh_pass
import _common as C

HOST = env("WG_HOST", required=True)
PWD = ssh_pass()
PORT = C.WG_PORT
WG_NET = C.WG_NET
SRV_IP = C.WG_SRV_IP
PHONE_IP = C.WG_PHONE_IP
NAS_IP = C.WG_NAS_IP
ENDPOINT = env("WG_ENDPOINT", required=True)
LAN = C.WG_LAN
NAS_LAN = C.WG_NAS_LAN

LOCAL_OUT = os.path.join(ROOT, "wireguard")


def conn():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, env("WG_USER", "root"), PWD, timeout=20,
              look_for_keys=False, allow_agent=False)
    return c


def run(c, cmd, timeout=120, show=True):
    si, so, se = c.exec_command(cmd, timeout=timeout)
    out = so.read().decode(errors="replace").strip()
    err = se.read().decode(errors="replace").strip()
    rc = so.channel.recv_exit_status()
    if show:
        text = out or err
        if text:
            print(text[:1500])
    return rc, out, err


c = conn()
stamp = time.strftime("%Y%m%d-%H%M%S")
bak = "/root/wg-backup-" + stamp

print("=" * 64)
print(">>> 1. 备份旧配置 ->", bak)
run(c, "mkdir -p %s && cp -a /etc/wireguard/. %s/ 2>/dev/null; ls %s" % (bak, bak, bak))

print("=" * 64)
print(">>> 2. 停止并禁用旧接口")
run(c, "systemctl disable --now wg-quick@wg0 2>&1; sleep 2; ip link show wg0 2>&1 || echo WG0_GONE")

print("=" * 64)
print(">>> 3. 清理 iptables 残留（只动 wg 相关规则，不碰 docker）")
run(c, r"""
set +e
# 反复删除，处理重复规则
for i in 1 2 3; do
  iptables -D FORWARD -i wg0 -j ACCEPT 2>/dev/null
  iptables -D FORWARD -o wg0 -j ACCEPT 2>/dev/null
done
# 移除宽泛的 -o eth0 MASQUERADE（旧配置遗留，会误伤 docker 流量）
iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE 2>/dev/null
echo "--- NAT 现状 ---"; iptables -t nat -S POSTROUTING
echo "--- FORWARD 现状 ---"; iptables -S FORWARD
""")

print("=" * 64)
print(">>> 4. 清空 /etc/wireguard 并生成密钥")
run(c, "rm -rf /etc/wireguard && mkdir -p /etc/wireguard && chmod 700 /etc/wireguard")

keys = {}
for name in ("srv", "phone", "nas"):
    _, priv, _ = run(c, "wg genkey", show=False)
    priv = priv.strip()
    _, pub, _ = run(c, "printf '%%s' '%s' | wg pubkey" % priv, show=False)
    pub = pub.strip()
    _, psk, _ = run(c, "wg genpsk", show=False)
    keys[name] = {"priv": priv, "pub": pub, "psk": psk.strip()}
    print("  %-6s pub=%s..." % (name, pub[:20]))

print("=" * 64)
print(">>> 5. 写入服务端配置")
server_conf = """[Interface]
Address = {srv_ip}/24
ListenPort = {port}
PrivateKey = {srv_priv}
MTU = 1420

PostUp   = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -A FORWARD -o wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -s {wg_net} -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -D FORWARD -o wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -s {wg_net} -o eth0 -j MASQUERADE

# ===== 手机 =====
[Peer]
PublicKey = {phone_pub}
PresharedKey = {phone_psk}
AllowedIPs = {phone_ip}/32

# ===== 远端 NAS =====
[Peer]
PublicKey = {nas_pub}
PresharedKey = {nas_psk}
AllowedIPs = {nas_ip}/32, {nas_lan}
""".format(
    srv_ip=SRV_IP, port=PORT, srv_priv=keys["srv"]["priv"], wg_net=WG_NET,
    phone_pub=keys["phone"]["pub"], phone_psk=keys["phone"]["psk"], phone_ip=PHONE_IP,
    nas_pub=keys["nas"]["pub"], nas_psk=keys["nas"]["psk"], nas_ip=NAS_IP, nas_lan=NAS_LAN,
)

sftp = c.open_sftp()
with sftp.file("/etc/wireguard/wg0.conf", "w") as f:
    f.write(server_conf)
sftp.close()
run(c, "chmod 600 /etc/wireguard/wg0.conf")

print("=" * 64)
print(">>> 6. 启动并设为开机自启")
run(c, "systemctl enable --now wg-quick@wg0 2>&1; sleep 3; systemctl is-active wg-quick@wg0")
run(c, "wg show")

print("=" * 64)
print(">>> 7. 本机连通性与规则检查")
run(c, "ip -4 addr show wg0 | grep inet")
run(c, "echo '--- NAT ---'; iptables -t nat -S POSTROUTING")
run(c, "echo '--- 路由 ---'; ip route | head -8")
run(c, "echo '--- 端口 ---'; ss -lnup | grep %d" % PORT)

print("=" * 64)
print(">>> 8. 生成客户端配置")
os.makedirs(LOCAL_OUT, exist_ok=True)

phone_conf = """[Interface]
PrivateKey = {priv}
Address = {ip}/32
MTU = 1420

[Peer]
PublicKey = {srv_pub}
PresharedKey = {psk}
Endpoint = {ep}
AllowedIPs = {wg_net}, {lan}, {nas_lan}
PersistentKeepalive = 25
""".format(priv=keys["phone"]["priv"], ip=PHONE_IP, srv_pub=keys["srv"]["pub"],
           psk=keys["phone"]["psk"], ep=ENDPOINT, wg_net=WG_NET, lan=LAN, nas_lan=NAS_LAN)

nas_conf = """[Interface]
PrivateKey = {priv}
Address = {ip}/32
MTU = 1420
PostUp   = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -A FORWARD -o wg0 -j ACCEPT
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -D FORWARD -o wg0 -j ACCEPT

[Peer]
PublicKey = {srv_pub}
PresharedKey = {psk}
Endpoint = {ep}
AllowedIPs = {wg_net}, {lan}
PersistentKeepalive = 25
""".format(priv=keys["nas"]["priv"], ip=NAS_IP, srv_pub=keys["srv"]["pub"],
           psk=keys["nas"]["psk"], ep=ENDPOINT, wg_net=WG_NET, lan=LAN)

for name, text in (("phone.conf", phone_conf), ("fn7567.conf", nas_conf)):
    with open(os.path.join(LOCAL_OUT, name), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
print("  phone.conf / fn7567.conf 已写入", LOCAL_OUT)

# 服务器配置也存一份本地
with open(os.path.join(LOCAL_OUT, "server-wg0.conf"), "w", encoding="utf-8", newline="\n") as f:
    f.write(server_conf)

print("=" * 64)
print(">>> 9. 生成手机二维码")
sftp = c.open_sftp()
with sftp.file("/tmp/phone.conf", "w") as f:
    f.write(phone_conf)
sftp.close()
run(c, "qrencode -t PNG -o /tmp/phone-qr.png -s 8 -m 2 -r /tmp/phone.conf && ls -l /tmp/phone-qr.png")
sftp = c.open_sftp()
sftp.get("/tmp/phone-qr.png", os.path.join(LOCAL_OUT, "phone-qr.png"))
sftp.close()
print("  phone-qr.png 已取回")

c.close()
print("=" * 64)
print("完成")

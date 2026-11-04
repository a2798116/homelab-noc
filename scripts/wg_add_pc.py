#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1) 在 WireGuard 服务端新增一个客户端 peer，热加载 + 持久化
2) 导出 .conf 到本地 wireguard/
3) 测试 hairpin NAT：从内网访问公网端点，看是否能环回到服务端

地址与端点全部来自 .env（WG_HOST / WG_ENDPOINT / WG_PC_IP / WG_LAN …）
"""
import os
import shlex
import time

import paramiko

from _common import (FNLAB_HOST, FNLAB_USER, ROOT, WG_LAN, WG_NAS_LAN, WG_NET,
                     WG_PC_IP, env, ssh_pass)

HOST = env("WG_HOST", required=True)
USER = env("WG_USER", "root")
PASS = ssh_pass()
LOCAL_OUT = os.path.join(ROOT, "wireguard")
PC_IP = WG_PC_IP
SERVER_ENDPOINT = env("WG_ENDPOINT", required=True)
SERVER_PUB = None


def conn():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, USER, PASS, timeout=20, look_for_keys=False, allow_agent=False)
    return c


def run(c, cmd, t=60):
    si, so, se = c.exec_command(cmd, timeout=t)
    out = so.read().decode(errors="replace").strip()
    err = se.read().decode(errors="replace").strip()
    return out or err


def main():
    c = conn()

    print("=" * 60)
    print(">>> 1. 检查是否已有 PC peer")
    wg0 = run(c, "cat /etc/wireguard/wg0.conf")
    if "# ===== Windows 本机" in wg0:
        print("  已存在 Windows peer，跳过生成（避免重复）")
        has_pc = True
    else:
        has_pc = False
        print("  不存在，准备生成")

    if not has_pc:
        print()
        print(">>> 2. 生成密钥对")
        priv = run(c, "wg genkey").strip()
        pub = run(c, "printf '%s' %s | wg pubkey" % ("'%s'", "'" + priv + "'")).strip()
        psk = run(c, "wg genpsk").strip()
        print("  pub = %s" % pub)
        print("  私钥已生成（不回显）")

        print()
        print(">>> 3. 热加载 peer（不断线）")
        sftp = c.open_sftp()
        with sftp.file("/tmp/pc.pub", "w") as f:
            f.write(pub)
        with sftp.file("/tmp/pc.psk", "w") as f:
            f.write(psk)
        sftp.close()
        run(c, "wg set wg0 peer $(cat /tmp/pc.pub) preshared-key /tmp/pc.psk allowed-ips %s/32" % PC_IP)
        print("  热加载完成")

        print()
        print(">>> 4. 写入 wg0.conf 持久化")
        block = (
            "\n# ===== Windows 本机 (办公笔记本)\n[Peer]\n"
            "PublicKey = %s\nPresharedKey = %s\nAllowedIPs = %s/32\n" % (pub, psk, PC_IP)
        )
        sftp = c.open_sftp()
        cur = run(c, "cat /etc/wireguard/wg0.conf")
        with sftp.file("/tmp/wg0.new", "w") as f:
            f.write(cur.rstrip("\n") + "\n" + block)
        sftp.close()
        run(c, "cp /etc/wireguard/wg0.conf /etc/wireguard/wg0.conf.bak.$(date +%s)")
        run(c, "install -m 600 -o root -g root /tmp/wg0.new /etc/wireguard/wg0.conf && rm -f /tmp/wg0.new /tmp/pc.pub /tmp/pc.psk")
        print("  已写入并备份旧配置")

        print()
        print(">>> 5. 生成 pc.conf")
        server_pub = wg0.split("PublicKey = ")[0]
        # 服务端公钥从 wg show 取
        server_pub = run(c, "wg show wg0 public-key").strip()
        pc_conf = """[Interface]
PrivateKey = {priv}
Address = {ip}/32
MTU = 1420

[Peer]
PublicKey = {spub}
PresharedKey = {psk}
Endpoint = {ep}
AllowedIPs = {wg_net}, {lan}, {nas_lan}
PersistentKeepalive = 25
""".format(priv=priv, ip=PC_IP, spub=server_pub, psk=psk, ep=SERVER_ENDPOINT,
                wg_net=WG_NET, lan=WG_LAN, nas_lan=WG_NAS_LAN)

        os.makedirs(LOCAL_OUT, exist_ok=True)
        p = os.path.join(LOCAL_OUT, "pc.conf")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(pc_conf)
        print("  已写到 %s" % p)

        # 同时备份到服务端，方便以后重发
        sftp = c.open_sftp()
        with sftp.file("/etc/wireguard/pc.conf", "w") as f:
            f.write(pc_conf)
        sftp.close()
        run(c, "chmod 600 /etc/wireguard/pc.conf")

    print()
    print("=" * 60)
    print(">>> 6. 当前 peer 列表")
    print(run(c, "wg show wg0 allowed-ips"))

    c.close()

    print()
    print("=" * 60)
    print(">>> 7. Hairpin NAT 测试")
    print("   在 10.9 抓包，同时从内网另一台机器发 UDP 到公网 51888")
    c = conn()
    run(c, "rm -f /tmp/hp.txt; nohup timeout 10 tcpdump -i any -n udp port 51888 > /tmp/hp.txt 2>&1 & sleep 2; echo started")

    # 从 10.32 发 UDP 包到公网 IP
    d = paramiko.SSHClient()
    d.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    d.connect(FNLAB_HOST, 22, FNLAB_USER, PASS, timeout=20, look_for_keys=False, allow_agent=False)
    ep_host, _, ep_port = SERVER_ENDPOINT.partition(":")
    cmd = ("for i in 1 2 3; do echo -n hairpin-test | timeout 2 nc -u -w1 %s %s "
           "2>/dev/null; done; echo sent" % (ep_host, ep_port or "51888"))
    si, so, se = d.exec_command("sudo -S -p '' bash -c %s" % shlex.quote(cmd), timeout=60)
    si.write(PASS + "\n")
    si.flush()
    print("  发包结果:", (so.read().decode(errors="replace") or se.read().decode(errors="replace")).strip())
    d.close()

    time.sleep(6)
    cap = run(c, "cat /tmp/hp.txt | grep -v '^tcpdump' | head -20")
    print()
    print("  10.9 抓包:")
    print("  " + (cap if cap.strip() else "(无任何包 —— hairpin 不通)"))
    c.close()


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""_settle 复核逻辑测试：模拟交换机 spd_act 偶发读 0"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "noc", "app"))
import config  # noqa
from collectors import switch as sw  # noqa

# 绕过"未配置 SWITCH_URL 就拒绝采集"的守卫（下面会替换 _fetch / _session）
config.SWITCH_URL = "http://switch.invalid"

ZERO = """
rx_rate:[100, 0, 0, 329, 826, 0, 0, 0]
tx_rate:[100, 0, 0, 346, 438, 0, 0, 0]
spd_act:[7, 0, 0, 7, 0, 0, 0, 0]
state:[1, 1, 1, 1, 1, 1, 0, 0]
is_poe_port:[1, 1, 0, 0, 0, 0, 0, 0]
power:[66, 0, 0, 0, 0, 0, 0, 0]
"""
OK = ZERO.replace("spd_act:[7, 0, 0, 7, 0,", "spd_act:[7, 0, 0, 7, 7,")

fails = []


def run(seq):
    """seq = 每次 _fetch 返回的页面"""
    box = {"i": 0}

    def fake_fetch(op, path):
        i = box["i"]
        box["i"] += 1
        return seq[min(i, len(seq) - 1)]

    sw._fetch = fake_fetch
    sw._session = lambda: None
    return sw.collect()


def check(name, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


print("== 1. 首次读 0、复核读回 2.5G → 修正，不标抖动 ==")
r = run([ZERO, OK])
p5 = [p for p in r["ports"] if p["no"] == 5][0]
check("口5 up=True", p5["up"] is True)
check("速率修正为 2.5G", p5["speed"] == "2.5G", p5["speed"])
check("不标抖动", p5["jitter"] is False)
check("jitter_count=0", r["jitter_count"] == 0)

print("== 2. 两次都读 0 → 沿用上次速率并标 ? 号，仍判 up ==")
sw._last_speed.clear()
r = run([OK])                       # 先喂一次正常，记住 2.5G
r = run([ZERO, ZERO])               # 这次两次都读 0
p5 = [p for p in r["ports"] if p["no"] == 5][0]
check("口5 up=True", p5["up"] is True)
check("速度显示 2.5G?", p5["speed"] == "2.5G?", p5["speed"])
check("标抖动", p5["jitter"] is True)
check("jitter_count=1", r["jitter_count"] == 1)

print("== 3. 真的没流量（rx=tx=0）→ 必须判 down ==")
DEAD = ZERO.replace("rx_rate:[100, 0, 0, 329, 826,", "rx_rate:[100, 0, 0, 329, 0,") \
                 .replace("tx_rate:[100, 0, 0, 346, 438,", "tx_rate:[100, 0, 0, 346, 0,")
r = run([DEAD, DEAD])
p5 = [p for p in r["ports"] if p["no"] == 5][0]
check("口5 up=False", p5["up"] is False)
check("不标抖动", p5["jitter"] is False)

print()
print("全部通过" if not fails else "失败 %d 项: %s" % (len(fails), fails))
sys.exit(1 if fails else 0)

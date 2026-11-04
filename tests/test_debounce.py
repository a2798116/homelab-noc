# -*- coding: utf-8 -*-
"""链路事件去抖 + spd_act 抖动判定的单元测试"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "noc", "app"))

import state  # noqa
from collectors import switch as sw  # noqa

P = lambda up, spd=None: {"no": 5, "up": up, "speed": "2.5G", "role": "iKuai 主干",
                          "spd_code": 7 if up else 0, "jitter": False}


def link_events():
    return [e for e in state._events if e.get("kind") == "link"]


def reset():
    state._events.clear()
    state._port_watch.clear()


fails = []


def check(name, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


print("== 1. 页面读数抖动（spd 0/7 交替）不该产生事件 ==")
reset()
state.update_switch({"ports": [P(True)]})          # 首次登记
for up in [False, True, False, True, False, True, False, True]:
    state.update_switch({"ports": [P(up)]})
check("0 条链路事件", len(link_events()) == 0, "实际 %d 条" % len(link_events()))

print("== 2. 真断链（连续 3 次 down）只报 1 次 ==")
reset()
state.update_switch({"ports": [P(True)]})
for _ in range(3):
    state.update_switch({"ports": [P(False)]})
ev = link_events()
check("1 条断开事件", len(ev) == 1 and "断开" in ev[0]["text"], str([e["text"] for e in ev]))

print("== 3. 恢复也只报 1 次（连续 3 次 up）==")
for _ in range(3):
    state.update_switch({"ports": [P(True)]})
ev = link_events()
check("共 2 条事件", len(ev) == 2, str([e["text"] for e in ev]))
check("最新一条是恢复（appendleft，ev[0] 最新）", "恢复" in ev[0]["text"], str(ev[0]["text"]))

print("== 4. 5 分钟内反复真翻 6 次 → 追加硬件告警 ==")
reset()
state.update_switch({"ports": [P(True)]})
for _ in range(8):                                  # 4 个来回 = 8 次翻转
    for _ in range(3):
        state.update_switch({"ports": [P(False)]})
    for _ in range(3):
        state.update_switch({"ports": [P(True)]})
texts = [e["text"] for e in link_events()]
check("出现「链路反复中断」", any("反复中断" in t for t in texts), str(texts[-3:]))
check("告警只发 1 次", sum(1 for t in texts if "反复中断" in t) == 1)

print("== 5. spd_act=0 但 rx/tx 有值 → 判 up + jitter，不判断开 ==")
html = """
rx_rate:[100, 0, 0, 0, 826, 0, 0, 0]
tx_rate:[100, 0, 0, 0, 438, 0, 0, 0]
spd_act:[7, 0, 0, 7, 0, 0, 0, 0]
state:[1, 1, 1, 1, 1, 1, 0, 0]
is_poe_port:[1, 1, 0, 0, 0, 0, 0, 0]
power:[66, 0, 0, 0, 0, 0, 0, 0]
"""
ports = sw._parse_main(html)
p5 = ports[4]
check("口5 up=True", p5["up"] is True)
check("口5 jitter=True", p5["jitter"] is True)
p4 = ports[3]
check("口4（spd=7 无流量）up=True jitter=False", p4["up"] and not p4["jitter"])
p2 = ports[1]
check("口2（spd=0 无流量）up=False", p2["up"] is False, "口2 是空置口")

print("== 6. 口6 管理启用但没插模块 → 仍是 down ==")
check("口6 up=False", ports[5]["up"] is False)

print()
print("全部通过" if not fails else "失败 %d 项: %s" % (len(fails), fails))
sys.exit(1 if fails else 0)

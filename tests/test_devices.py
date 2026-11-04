# -*- coding: utf-8 -*-
"""终端上下线降噪测试：交叉验证 / 宽限期 / 事件抑制 / 探活补位

不依赖 config.local.json —— 测试自己注入合成资产表，
地址前缀取自 config.LAN_PREFIX，因此换任何内网段都能跑。

运行：python tests/test_devices.py
"""
import os
import sys
import time as _t

_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "noc", "app")
sys.path.insert(0, _APP)

import assets  # noqa: E402
import config  # noqa: E402
import state  # noqa: E402

# ---- 合成资产表：把测试用到的那几台设备登记进去 ----
PVE = config.LAN_PREFIX + "10"
PHONE = config.LAN_PREFIX + "99"
SWITCH = config.LAN_PREFIX + "3"
PVE_NAME = "PVE 宿主机"

assets.DEVICE_META.clear()
assets.DEVICE_META.update({
    PVE: {"name": PVE_NAME, "zone": "机房", "icon": "server", "cat": "infra",
          "hw": "", "note": ""},
    PHONE: {"name": "手机", "zone": "移动", "icon": "phone", "cat": "client",
            "hw": "", "note": ""},
    SWITCH: {"name": "交换机", "zone": "机房", "icon": "switch", "cat": "net",
             "hw": "", "note": ""},
})

# 冻结时间
clock = [1_000_000.0]
state.time.time = lambda: clock[0]

fails = []


def check(name, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


def dev(ip, mac="aa:bb:cc:00:11:22"):
    return {"ip": ip, "mac": mac, "hostname": "", "vendor": "", "model": "",
            "comment": "", "type": "", "conn": 0, "up": 0, "down": 0,
            "up_total": 0, "down_total": 0, "today_total": 0, "uptime": "",
            "static": False, "iface": ""}


def ipse():
    return [d["ip"] for d in state._state["devices"]]


def ev(kind):
    return [e["text"] for e in state._events if e.get("kind") == kind]


check("宽限期 %.0fs / 抑制 %.0fs / 网段 %s"
      % (config.DEV_GRACE, config.DEV_REPEAT, config.LAN_CIDR), True)

print("== 1. 首轮不播报 ==")
state.update_devices([dev(PVE, "aa:00:00:00:00:10"), dev(PHONE)])
check("0 条事件", len(state._events) == 0, "实际 %d" % len(state._events))

print("== 2. 网关表里消失，但探测可达 → 保持在线、不报离线 ==")
state._state["probes"] = [{"ip": PVE, "up": True, "rtt": 0.2}]
clock[0] += 5
state.update_devices([dev(PHONE)])
d = next((x for x in state._state["devices"] if x["ip"] == PVE), None)
check("PVE 仍在列表", d is not None)
check("ikuai_seen=False", d and d.get("ikuai_seen") is False)
check("不算 stale", d and d["stale"] is False)
check("0 条离线事件", len(ev("offline")) == 0, str(ev("offline")))

print("== 3. 手机消失 → 先标 stale，不报事件 ==")
clock[0] += 5
state.update_devices([])
d = next((x for x in state._state["devices"] if x["ip"] == PHONE), None)
check("手机仍在列表", d is not None)
check("stale=True", d and d.get("stale") is True)
check("0 条离线事件", len(ev("offline")) == 0, str(ev("offline")))
print("     宽限期内 online 计数应排除 stale：",
      sum(1 for x in state._state["devices"] if not x.get("stale")), "台在线")

print("== 4. 超过宽限期 → 报 1 条离线并移除 ==")
clock[0] += config.DEV_GRACE + 2
state.update_devices([])
check("1 条离线事件", len(ev("offline")) == 1, str(ev("offline")))
check("手机已移除", PHONE not in ipse())
check("PVE 仍在（探测活着）", PVE in ipse())

print("== 5. 抑制窗口内再次消失不重复播报 ==")
clock[0] += 5
state.update_devices([dev(PHONE)])          # 回来
state.update_devices([])                     # 又走
clock[0] += config.DEV_GRACE + 2
state.update_devices([])
check("离线事件仍是 1 条", len(ev("offline")) == 1, str(ev("offline")))

print("== 6. 探测也断了 → 按正常流程报离线 ==")
state._state["probes"] = [{"ip": PVE, "up": False, "rtt": None}]
state.update_devices([dev(PVE, "aa:00:00:00:00:10"), dev(PHONE)])
clock[0] += config.DEV_REPEAT + 1
for _ in range(3):
    clock[0] += config.DEV_GRACE
    state.update_devices([dev(PHONE)])
check("PVE 出现在离线事件里", any(PVE_NAME + " 离线" in t for t in ev("offline")), str(ev("offline")))

print("== 7. 网关从不报的设备（交换机），探活可达就补进终端墙 ==")
state._state["devices"] = []
state._events.clear()
state._dev_gone.clear()
state._dev_event_ts.clear()
state._dev_last_seen.clear()
state._dev_offline.clear()
state._state["probes"] = [{"ip": SWITCH, "up": True, "rtt": 0.5},
                          {"ip": "223.5.5.5", "up": True, "rtt": 3}]
clock[0] += 10
state.update_devices([dev(PHONE)])
ips = ipse()
check("交换机被补进列表", SWITCH in ips, str(ips))
check("公网探测点没被当终端", "223.5.5.5" not in ips)
d = next((x for x in state._state["devices"] if x["ip"] == SWITCH), None)
check("标 ikuai_seen=False", d and d["ikuai_seen"] is False)
check("首轮不报上线", len(ev("online")) == 0, str(ev("online")))

clock[0] += 10
state.update_devices([dev(PHONE)])
check("第二轮仍在且仍不报上线", SWITCH in ipse() and len(ev("online")) == 0)

print("== 8. 网关漏报一轮又报回来 → 静默，不播事件 ==")
state._state["devices"] = []
state._events.clear()
state._dev_gone.clear()
state._dev_event_ts.clear()
state._dev_last_seen.clear()
state._dev_offline.clear()
state._state["probes"] = [{"ip": PVE, "up": True, "rtt": 0.2}]
clock[0] += 100
state.update_devices([dev(PVE, "aa:00:00:00:00:10")])
state.update_devices([])
check("漏报期间 0 条事件", len(state._events) == 0, str([e["text"] for e in state._events]))
clock[0] += 60
state.update_devices([dev(PVE, "aa:00:00:00:00:10")])
check("恢复上报也静默", len(state._events) == 0, str([e["text"] for e in state._events]))
d = next((x for x in state._state["devices"] if x["ip"] == PVE), None)
check("ikuai_seen 回到 True", d and d["ikuai_seen"] is True)

print("== 9. 真离线（探测也断）才播离线，回来再播上线 ==")
state._state["probes"] = [{"ip": PVE, "up": False, "rtt": None}]
state.update_devices([dev(PVE, "aa:00:00:00:00:10"), dev(PHONE)])
clock[0] += config.DEV_REPEAT + 1
for _ in range(3):
    clock[0] += config.DEV_GRACE
    state.update_devices([dev(PHONE)])
check("播 1 条离线", len(ev("offline")) == 1, str(ev("offline")))
clock[0] += config.DEV_REPEAT + 1
state.update_devices([dev(PVE, "aa:00:00:00:00:10"), dev(PHONE)])
pve_on = [t for t in ev("online") if "PVE" in t]
check("PVE 回来播 1 条上线", len(pve_on) == 1, str(ev("online")))
clock[0] += 10
state.update_devices([dev(PVE, "aa:00:00:00:00:10"), dev(PHONE)])
check("不重复播报", len([t for t in ev("online") if "PVE" in t]) == 1, str(ev("online")))

print()
print("全部通过" if not fails else "失败 %d 项: %s" % (len(fails), fails))
sys.exit(1 if fails else 0)

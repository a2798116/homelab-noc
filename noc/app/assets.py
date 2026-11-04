# -*- coding: utf-8 -*-
"""全屋静态资产表：名字、位置、图标、分类

数据本身不写在代码里 —— 从 config.local.json 的 devices 段读取
（模板见 config.example.json）。表里没有的设备会被自动推断。

IP 是索引键。
"""
import config

# IP → {"name","zone","icon","cat","hw","note"}
DEVICE_META = {}
for _ip, _m in (config._sec("devices") or {}).items():
    if isinstance(_m, dict) and _m.get("name"):
        DEVICE_META[str(_ip)] = _m

CATEGORY_ORDER = ["infra", "net", "media", "smart", "client"]
CATEGORY_LABEL = {
    "infra": "基础设施",
    "net": "网络设备",
    "media": "影音数码",
    "smart": "智能家居",
    "client": "个人终端",
}

# 拓扑节点的显示文字（前端内置一套通用标签，启动后由这里覆盖）
TOPOLOGY = config.TOPOLOGY_LABELS

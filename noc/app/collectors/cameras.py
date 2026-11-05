# -*- coding: utf-8 -*-
"""安防摄像头：把 config 里的 RTSP 源注册进本机 go2rtc

浏览器不能直接播 RTSP，由 go2rtc 做转封装。这里只负责「注册流」这一件事：
  * 原始流          → /api/frame.jpeg 抽一帧做快照网格，H265 也能用
  * ffmpeg 转码流   → H265 转 H264，/api/stream.mp4 才能被 Chrome 播放

go2rtc 重启后动态流会丢，所以按「缺谁补谁」的方式定期自愈；
已存在的流不重复 PUT，否则会把正在播放的连接踢掉。
"""
import json
import urllib.parse
import urllib.request

import config


def _url(path, **params):
    return config.CAM_URL + path + ("?" + urllib.parse.urlencode(params) if params else "")


def _streams():
    """go2rtc 当前已注册的流名集合"""
    with urllib.request.urlopen(_url("/api/streams"), timeout=8) as r:
        data = json.loads(r.read() or b"{}")
    return set(data.keys()) if isinstance(data, dict) else set()


def _put(name, src):
    req = urllib.request.Request(_url("/api/streams", name=name, src=src), method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def _pairs():
    for c in config.CAMERAS:
        rtsp = c.get("rtsp")
        if not rtsp:
            continue          # src 直接指向 go2rtc 里已有的流，不用注册
        yield c["src"], rtsp
        if c.get("transcode"):
            yield c["live"], "ffmpeg:" + rtsp + "#video=h264#audio=copy"


def sync_streams():
    """缺的流补上，返回本次新增的流名"""
    try:
        have = _streams()
    except Exception as e:
        return {"ok": False, "error": str(e)[:120], "added": []}
    added = []
    for name, src in _pairs():
        if name in have:
            continue
        try:
            _put(name, src)
            added.append(name)
        except Exception:
            pass
    return {"ok": True, "added": added}


def public_list():
    """给前端的清单：不下发 RTSP 地址（里面通常带账号密码）"""
    return {
        "interval": config.CAM_SNAP_MS,
        "cameras": [{
            "id": c["id"], "title": c["title"], "note": c["note"],
            "snap": c["src"], "live": c["live"],
            "blank": bool(c.get("blank")),       # 空槽位：只占位，不发请求
        } for c in config.CAMERAS],
    }

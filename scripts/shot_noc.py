#!/usr/bin/env python3
"""用本机 headless Chrome 给 NOC 大屏截图，供布局自检。

为什么不能直接用 `chrome --headless --screenshot`：
  1) 不指定 --user-data-dir 时，Chrome 会把请求转交给日常正在运行的实例，
     命令直接挂住不返回（表现为超时被杀）。
  2) 加 --virtual-time-budget 会冻结网络与定时器，而 NOC 页面有一条 SSE
     长连接（/api/stream）。虚拟时间耗尽时渲染进程被强制终止，报
     "Abnormal renderer termination" 且不产出文件。
  以上两条都在 2026-10-31 实测踩过，本脚本已绕开。

用法：
    python scripts/shot_noc.py                        # 默认截 NOC 大屏
    python scripts/shot_noc.py --width 1600 --height 900
    python scripts/shot_noc.py --out D:/tmp/a.png
    python scripts/shot_noc.py --url http://192.168.1.10:8930/   # 截旧看板
    python scripts/shot_noc.py --theme light          # 注入浅色主题

中心节点地址请用 --url 指定，或设环境变量 NOC_URL（默认值是占位示例，
不是任何真实主机）。默认输出：<仓库根>/shots/noc-<时间戳>.png
（shots/ 已在 .gitignore）
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

DEFAULT_URL = os.environ.get("NOC_URL", "http://192.168.1.10:8940/")


def find_browser() -> str:
    for p in CHROME_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    sys.exit("找不到 Chrome / Edge，请用 --browser 指定可执行文件路径")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="headless 截图 NOC 大屏")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"目标地址（默认 {DEFAULT_URL}）")
    ap.add_argument("--out", help="输出 PNG 路径（默认 shots/noc-<时间戳>.png）")
    ap.add_argument("--width", type=int, default=1920, help="视口宽（默认 1920）")
    ap.add_argument("--height", type=int, default=1080, help="视口高（默认 1080）")
    ap.add_argument("--scale", type=float, default=1.0, help="设备像素比（默认 1）")
    ap.add_argument("--theme", choices=["dark", "light"], help="强制主题（改写 localStorage 后重载）")
    ap.add_argument("--browser", help="浏览器可执行文件路径")
    ap.add_argument("--timeout", type=int, default=60, help="超时秒数（默认 60）")
    args = ap.parse_args()

    browser = args.browser or find_browser()

    if args.out:
        out = Path(args.out).expanduser().resolve()
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = root / "shots" / f"noc-{stamp}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    # 固定复用同一个 profile：既避免和日常 Chrome 单实例冲突，又能保住缓存
    profile = Path(tempfile.gettempdir()) / "noc-shot-profile"
    profile.mkdir(parents=True, exist_ok=True)

    cmd = [
        browser,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,OptimizationHints",
        f"--user-data-dir={profile}",
        f"--window-size={args.width},{args.height}",
        f"--force-device-scale-factor={args.scale}",
        f"--screenshot={out}",
    ]
    url = args.url
    if args.theme:
        # 页面支持 ?theme=light|dark（index.html 首屏脚本优先读它）
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}theme={args.theme}"

    print(f"浏览器  {browser}")
    print(f"目标    {url}")
    print(f"视口    {args.width}x{args.height} @{args.scale}x")
    print(f"输出    {out}")

    cmd.append(url)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=args.timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        sys.exit(f"超时 {args.timeout}s：页面没渲染完。可加大 --timeout，"
                 f"或确认 {args.url} 可达")

    err = "\n".join(
        ln for ln in (r.stderr or "").splitlines()
        if "update_service_dialer" not in ln
        and "bytes written to file" not in ln
        and ln.strip()
    )
    if err:
        print("--- 浏览器日志 ---")
        print(err[-1200:])

    if not out.exists() or out.stat().st_size == 0:
        sys.exit("截图失败：没有产出文件。若日志出现 Abnormal renderer termination，"
                 "先确认命令里没有 --virtual-time-budget（它和 SSE 长连接冲突）")

    print(f"OK  {out}  ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

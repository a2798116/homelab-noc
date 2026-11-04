#!/bin/sh
# Homelab Monitor 采集端一键安装（Linux / fnOS / Debian / Ubuntu）
#
# 用法（在目标设备上以 root 执行）：
#   curl -fsSL http://<中心地址>/agent/install.sh | \
#     sudo SERVER=http://<中心地址> TOKEN=<设备令牌> MODEL="型号" sh
#
# 可选环境变量：
#   NAME      展示名，默认取 hostname
#   MODEL     设备型号描述
#   INTERVAL  上报间隔，默认 5min（支持 1min/30s 等 systemd 时间格式）
#
# 卸载：
#   systemctl disable --now fnos-monitor-report.timer
#   rm -f /etc/systemd/system/fnos-monitor-report.service \
#         /etc/systemd/system/fnos-monitor-report.timer \
#         /usr/local/bin/fnos-monitor-report.py /etc/fnos-monitor.env
#   systemctl daemon-reload
set -eu

: "${SERVER:?缺少 SERVER，例如 SERVER=http://<中心地址>:8930}"
: "${TOKEN:?缺少 TOKEN（设备令牌）}"

NAME="${NAME:-$(hostname 2>/dev/null || echo unknown)}"
MODEL="${MODEL:-}"
INTERVAL="${INTERVAL:-5min}"
MOUNTS="${MOUNTS:-}"
BASE="$(printf '%s' "$SERVER" | sed 's:/*$::')"

PY=""
for c in /usr/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done
if [ -z "$PY" ]; then
  echo "[错误] 未找到 python3，请先安装：apt install -y python3" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "[1/4] 下载采集脚本 -> $BASE/agent/fnos-monitor-report.py"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$BASE/agent/fnos-monitor-report.py" -o "$TMP/agent.py"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$TMP/agent.py" "$BASE/agent/fnos-monitor-report.py"
else
  echo "[错误] 需要 curl 或 wget" >&2
  exit 1
fi
install -m 0755 "$TMP/agent.py" /usr/local/bin/fnos-monitor-report.py
echo "      已安装到 /usr/local/bin/fnos-monitor-report.py"

echo "[2/4] 写入配置 /etc/fnos-monitor.env"
umask 077
cat > /etc/fnos-monitor.env <<EOF
SERVER=$BASE
TOKEN=$TOKEN
DEVICE_NAME=$NAME
DEVICE_MODEL=$MODEL
${MOUNTS:+MOUNTS=$MOUNTS}
EOF
chmod 600 /etc/fnos-monitor.env

echo "[3/4] 注册定时任务"
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  cat > /etc/systemd/system/fnos-monitor-report.service <<EOF
[Unit]
Description=Homelab Monitor 采集上报
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$PY /usr/local/bin/fnos-monitor-report.py
StandardOutput=journal
StandardError=journal
EOF

  cat > /etc/systemd/system/fnos-monitor-report.timer <<EOF
[Unit]
Description=定时上报设备状态到 Homelab Monitor

[Timer]
OnBootSec=90s
OnUnitActiveSec=$INTERVAL
AccuracySec=20s
Unit=fnos-monitor-report.service

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable --now fnos-monitor-report.timer >/dev/null 2>&1
  echo "      systemd timer 已启用（间隔 $INTERVAL）"
else
  LINE="*/5 * * * * $PY /usr/local/bin/fnos-monitor-report.py >/dev/null 2>&1"
  if command -v crontab >/dev/null 2>&1; then
    ( crontab -l 2>/dev/null | grep -v 'fnos-monitor-report.py' ; echo "$LINE" ) | crontab -
    echo "      crontab 已写入（每 5 分钟）"
  else
    echo "      [警告] 无 systemd 也无 crontab，需自行调度" >&2
  fi
fi

echo "[4/4] 立即上报一次"
if "$PY" /usr/local/bin/fnos-monitor-report.py; then
  echo ""
  echo "✅ 接入完成：$NAME -> $BASE"
  echo "   查看数据：浏览器打开 $BASE （需在内网）"
else
  echo ""
  echo "⚠️  首次上报失败。常见原因："
  echo "   1. TOKEN 不在中心的白名单里 —— 需要先在中心登记该设备"
  echo "   2. 中心地址不可达 —— 测试：curl -s $BASE/api/health"
  echo "   3. 公网访问时只有 /api/report 开放，属正常"
  exit 1
fi

#!/bin/bash
# 投递中台看板自启（SQLite 版）：可由登录自启项 / 计划任务调用（配置方法见 README）。
# 端口读 config.json「端口」（默认 8790）；已在运行则跳过，否则后台拉起本目录的 中台看板.py。
# 看板只绑 127.0.0.1（仅本机访问）。
cd "$(dirname "$0")" || exit 1
LOGDIR="$(pwd)/logs"; mkdir -p "$LOGDIR"
# 简单轮转：超过 5MB 归档为 .old
[ -f "$LOGDIR/dashboard.log" ] && [ "$(stat -c%s "$LOGDIR/dashboard.log")" -gt 5242880 ] && mv "$LOGDIR/dashboard.log" "$LOGDIR/dashboard.log.old"
PORT="$(python3 - <<'PY'
import json
try:
    print(json.load(open("config.json", encoding="utf-8")).get("端口") or 8790)
except Exception:
    print(8790)
PY
)"
PORT="${PORT:-8790}"
if curl -s --noproxy '*' --max-time 3 -o /dev/null "http://127.0.0.1:${PORT}/ok"; then
  echo "$(date '+%F %T') 自启检查: ${PORT} 已在运行，跳过" >> "$LOGDIR/dashboard.log"
  exit 0
fi
nohup python3 "$(pwd)/中台看板.py" >> "$LOGDIR/dashboard.log" 2>&1 &
echo "$(date '+%F %T') 自启: 已拉起 ${PORT} 看板（SQLite，127.0.0.1）" >> "$LOGDIR/dashboard.log"

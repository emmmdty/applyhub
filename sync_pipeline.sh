#!/bin/bash
# 秋招中台定时链（SQLite 版）：mail.py --sync -> sync.py -> SQLite 备份滚动 -> 看板看护
# 由系统计划任务每 2 小时调用（Windows/macOS/Linux 配置方法见 README「自动定时同步」）。
# 日志统一追加到 logs/last_run.log（>5MB 自动归档 .old）。
# 端口 / 备份目录可在 config.json 里改（网页「设置」页可视化编辑）。

cd "$(dirname "$0")" || exit 1
LOGDIR="$(pwd)/logs"; mkdir -p "$LOGDIR"
[ -f "$LOGDIR/last_run.log" ] && [ "$(stat -c%s "$LOGDIR/last_run.log")" -gt 5242880 ] && mv "$LOGDIR/last_run.log" "$LOGDIR/last_run.log.old"

LOG="$LOGDIR/last_run.log"
mkdir -p "$(dirname "$LOG")"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

# 从 config.json 读端口与备份目录（缺失/损坏回退默认值，与 grist_store.get_config 同语义）
read_cfg() { python3 - "$1" <<'PY'
import sys, json, os
try:
    c = json.load(open("config.json", encoding="utf-8"))
except Exception:
    c = {}
v = c.get(sys.argv[1])
print("" if v is None else v)
PY
}
PORT="$(read_cfg 端口)"; PORT="${PORT:-8790}"
BKDIR_CFG="$(read_cfg 备份目录)"
if [ -n "$BKDIR_CFG" ]; then
  case "$BKDIR_CFG" in /*) BKDIR="$BKDIR_CFG" ;; *) BKDIR="$(pwd)/$BKDIR_CFG" ;; esac
else
  BKDIR="$(pwd)/backups"
fi

dash_alive() { curl -s --noproxy '*' --max-time 3 -o /dev/null "http://127.0.0.1:${PORT}/ok"; }

log "===== 定时链开始（SQLite，端口 $PORT） ====="

MAIL_OK=0; SYNC_OK=0; BACKUP_OK=0

# 步骤1：邮件拉取 -> 规则抽取 -> 写 SQLite（投递记录+邮件存档+待确认队列）
log "步骤1 邮件拉取: python3 mail.py --sync 开始"
python3 mail.py --sync >> "$LOG" 2>&1 && MAIL_OK=1
log "步骤1 邮件拉取: 结束（$([ "$MAIL_OK" -eq 1 ] && echo 成功 || echo 失败)）"

# 步骤2：SQLite -> Obsidian 笔记 frontmatter 刷新
log "步骤2 数据同步: python3 sync.py 开始"
python3 sync.py >> "$LOG" 2>&1 && SYNC_OK=1
log "步骤2 数据同步: 结束（$([ "$SYNC_OK" -eq 1 ] && echo 成功 || echo 失败)）"

# 步骤3：SQLite 备份（在线一致性快照）+ 滚动保留最近 30 份（失败不中断）
BK="$BKDIR/中台-sqlite-$(date +%Y%m%d-%H%M).sqlite"
log "步骤3 备份: 复制到 $BK"
mkdir -p "$BKDIR"
python3 - "$BK" >> "$LOG" 2>&1 <<'PY'
import sys
from grist_store import download_backup
download_backup(sys.argv[1])
print("备份完成:", sys.argv[1])
PY
if [ $? -eq 0 ]; then
  BACKUP_OK=1
  ls -1t "$BKDIR"/中台-sqlite-*.sqlite 2>/dev/null | tail -n +31 | xargs -r rm -f
  log "步骤3 备份: OK（滚动保留最近 30 份）"
else
  log "步骤3 备份: 失败（不中断，详情见上方输出）"
fi

# 步骤4：可视化看板看护——没起就后台拉起（失败不中断；看板只绑 127.0.0.1）
if ! dash_alive; then
  log "步骤4 看板: ${PORT} 未响应，后台拉起 中台看板.py"
  nohup python3 "$(pwd)/中台看板.py" >> "$LOG" 2>&1 &
fi

if [ "$MAIL_OK" -eq 1 ] && [ "$SYNC_OK" -eq 1 ]; then exit 0; fi
exit 1

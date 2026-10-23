#!/bin/sh
# 每日备份：bot.db（sqlite 在线备份，防 WAL 撕裂）+ 登录态/收益快照等 data 文件。
# 装机：cp 到 /opt/ks-bot/deploy/ && chmod +x，加 /etc/cron.d/ks-backup 一行。
# 保留最近 7 份，出问题回滚：把对应 tgz 解开覆盖 data/ 即可。
set -e
KS_HOME="${KS_HOME:-/opt/ks-bot}"
KEEP=7
DEST=/root/ks-backups
PY="$KS_HOME/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

mkdir -p "$DEST"
STAMP=$(date +%F)

# sqlite 在线备份（不能直接 tar 一个 WAL 库：可能拷到半写状态）
"$PY" - "$KS_HOME/data/bot.db" "$DEST/bot-$STAMP.db" <<'EOF'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
sqlite3.connect(src).backup(sqlite3.connect(dst))
EOF

# data 里除库以外的关键小文件（登录态、cookies、收益快照、任务池）
tar czf "$DEST/data-$STAMP.tgz" -C "$KS_HOME/data" \
    --exclude='bot.db*' --exclude='*.log' . 2>/dev/null || true

# 轮转：只留最近 KEEP 份
ls -1t "$DEST"/bot-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
ls -1t "$DEST"/data-*.tgz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

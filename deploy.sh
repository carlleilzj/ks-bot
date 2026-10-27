#!/bin/bash
# 一键部署：本机工作区 → 日本协调端（gc-jp）+ 阿里云发布端。
# 用法：./deploy.sh [japan|aliyun|all]   （默认 all；兼容旧参数 germany=同 japan）
# 流程：tar 同步代码（不动 .env/data/media/logs）→ 协调端跑全量测试
#       → 两边 systemctl restart。测试失败即中止，不重启线上。
# 2026-10-28：德国机已退租删机，协调端改为 gc-jp（62.106.75.207）。
# 注意：目标机 ks-bot 以 ksbot 用户跑（见 deploy/ksbot-vps.service 头部注释），
#       同步后代码树需保持可读（tar 覆盖不会破坏 o+r，但本地新建文件 600 除外）。
set -e
cd "$(dirname "$0")"

TARGET="${1:-all}"
FILES=$(git ls-files 'bot/**' 'tests/**' 'deploy/**' | grep -v -E 'cookies|state')

sync_to() {
    local host="$1"
    echo "== 同步代码到 $host =="
    COPYFILE_DISABLE=1 tar -cf - $FILES | ssh -o ConnectTimeout=15 "$host" \
        'cd /opt/ks-bot && tar -xf - && find . -name "._*" -not -path "./.git/*" -delete && chmod -R o+rX bot tests deploy'
}

case "$TARGET" in
  japan|germany|all)
    sync_to gc-jp
    echo "== 协调端跑测试 =="
    ssh gc-jp 'cd /opt/ks-bot && .venv/bin/python -m pytest tests/ -q -p no:cacheprovider'
    ;;
esac

case "$TARGET" in
  aliyun|all)
    sync_to aliyun-gz
    ;;
esac

echo "== 重启服务 =="
[ "$TARGET" = aliyun ] || ssh gc-jp 'systemctl restart ks-bot.service && sleep 2 && systemctl is-active ks-bot.service'
[ "$TARGET" = aliyun ] && ssh aliyun-gz 'systemctl restart ksbot-publish.service && sleep 2 && systemctl is-active ksbot-publish.service'
echo "完成。"

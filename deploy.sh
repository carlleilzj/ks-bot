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
FILES=$(git ls-files 'bot/**' 'tests/**' 'deploy/**' 'tools/**' 'config.yaml' 'pyproject.toml' 'requirements.txt' | grep -v -E 'cookies|state')

sync_to() {
    local host="$1"
    echo "== 同步代码到 $host =="
    # 注意：macOS 的 tar 会保留本机 uid（501:staff），传到 Linux 后会变成
    # 陌生属主，导致以 ksbot 用户运行的服务读不到 config.yaml。
    # 因此同步后统一 chown 回目标机的服务用户。
    #
    # 2026-11-09 补：tar 只覆盖不删除，所以**本地删除/改名的文件会永远残留在
    # 线上**。本次就因此踩到：tests/test_banned_tags.py 本地已删，线上还在跑，
    # 部署时 14 个用例报错、部署中止。
    # 处理方式：把本次文件清单送到远端，删掉 bot/ tests/ 下不在清单里的 .py。
    # 只碰 .py，不动 data/media/logs/env，避免误删运行数据。
    COPYFILE_DISABLE=1 tar -cf - $FILES | ssh -o ConnectTimeout=15 "$host" \
        'cd /opt/ks-bot && tar -xf - && find . -name "._*" -not -path "./.git/*" -delete \
         && chown -R ksbot:ksbot bot tests deploy tools config.yaml 2>/dev/null || true; \
         chmod -R o+rX bot tests deploy tools; chmod o+r config.yaml 2>/dev/null; true'

    printf '%s\n' $FILES > /tmp/ks_deploy_manifest.txt
    ssh -o ConnectTimeout=15 "$host" 'cat > /tmp/ks_deploy_manifest.txt' \
        < /tmp/ks_deploy_manifest.txt
    ssh -o ConnectTimeout=15 "$host" \
        'cd /opt/ks-bot && find bot tests -name "*.py" -type f | while read -r f; do \
           rel="${f#./}"; \
           grep -qxF "$rel" /tmp/ks_deploy_manifest.txt \
             || { echo "  清理线上残留: $rel"; rm -f "$f"; }; \
         done; true'
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
case "$TARGET" in
  japan|germany)
    ssh gc-jp 'systemctl restart ks-bot.service && sleep 2 && systemctl is-active ks-bot.service'
    ;;
  aliyun)
    ssh aliyun-gz 'systemctl restart ksbot-publish.service && sleep 2 && systemctl is-active ksbot-publish.service'
    ;;
  all)
    ssh gc-jp 'systemctl restart ks-bot.service && sleep 2 && systemctl is-active ks-bot.service'
    ssh aliyun-gz 'systemctl restart ksbot-publish.service && sleep 2 && systemctl is-active ksbot-publish.service'
    ;;
esac
echo "完成。"

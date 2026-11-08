#!/bin/bash
# 自建 Telegram Bot API Server（解除 20MB 文件上限，本机零拷贝取文件）
#
# 为什么值得自建（不是折腾）：
#   1. 官方 api.telegram.org 的 getFile 下载上限是 20MB —— 短视频一不小心就超了；
#   2. 自建 + --local 模式上限 2000MB；
#   3. 最关键：--local 模式下 getFile 返回的是**服务器本机绝对路径**，
#      我们可以硬链接（零拷贝、零网络传输）直接进流水线，比官方通道快得多。
#
# 前置（一次性，必须人工做）：
#   打开 https://my.telegram.org/apps 创建应用，拿到 api_id / api_hash
#
# 用法：
#   ./deploy/setup-bot-api.sh <api_id> <api_hash>
#
set -euo pipefail

API_ID="${1:-}"
API_HASH="${2:-}"
DATA_DIR="/opt/tg-bot-api"
PORT=8081

if [ -z "$API_ID" ] || [ -z "$API_HASH" ]; then
    echo "用法：$0 <api_id> <api_hash>"
    echo "申请地址：https://my.telegram.org/apps"
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "✗ 未安装 docker"
    exit 1
fi

echo "== 1. 准备数据目录 =="
mkdir -p "$DATA_DIR/data"
chown -R 1000:1000 "$DATA_DIR" 2>/dev/null || true

echo "== 2. 启动 Bot API server =="
# --local           解除限制（关键）
# --api-id/hash     你的应用凭据
# --dir             数据目录（bot 会话与文件缓存）
# --http-port       监听端口（只绑 127.0.0.1，不对外）
docker rm -f tg-bot-api 2>/dev/null || true

docker run -d \
    --name tg-bot-api \
    --restart unless-stopped \
    -p 127.0.0.1:${PORT}:8081 \
    -v "${DATA_DIR}/data:/var/lib/telegram-bot-api" \
    -e TELEGRAM_API_ID="${API_ID}" \
    -e TELEGRAM_API_HASH="${API_HASH}" \
    -e TELEGRAM_LOCAL=1 \
    aiogram/telegram-bot-api:latest

echo "== 3. 等待就绪 =="
for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
        echo "   ✓ 服务已就绪（端口 ${PORT}）"
        break
    fi
    sleep 1
done

echo
echo "== 4. 验证 =="
docker ps --filter name=tg-bot-api --format "  {{.Status}}  {{.Ports}}"
echo
echo "服务日志（前 20 行）："
docker logs tg-bot-api 2>&1 | tail -20

cat <<EOF

============================================================
自建 Bot API 已启动：http://127.0.0.1:${PORT}

**最后一步必做**（否则机器人收不到消息）：
  在 .env 里改 TELEGRAM_API_BASE=http://127.0.0.1:${PORT}

然后把 bot 从官方服务器「注销」（local server 与官方服务器不能同时登录）：
  curl -X POST "https://api.telegram.org/bot<TOKEN>/logOut"
  # 然后重启 ks-bot 服务

验证是否生效：
  python -m bot.main --ig-check     # 若报错说明配置有问题
  发一个视频文件给 bot，回执里若显示 MB 数 > 20 仍能入库 = 成功

回滚到官方：
  在 .env 删掉 TELEGRAM_API_BASE（或改回 https://api.telegram.org），
  然后 docker rm -f tg-bot-api && systemctl restart ks-bot.service
============================================================
EOF

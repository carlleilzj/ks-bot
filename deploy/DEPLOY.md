# 分体部署指南：VPS 采集处理 + 家庭端发布

## 架构

```
┌───────────── 香港 VPS（生产者）─────────────┐      ┌────────── 家庭旧电脑（发布者）──────────┐
│ systemd: ksbot-vps.service                  │      │ systemd: ksbot-tunnel.service (SSH隧道) │
│  bot.main --stage=process                   │      │ systemd: ksbot-publish.service          │
│  发现层→下载→转码→gemini质检→ASR→文案→字幕 │ ───▶ │  bot.publish_worker                     │
│  → READY 停，任务带 publish_jobs            │ SSH  │  认领→sha256校验下载→Playwright→回报   │
│  bot/remote_api.py 绑 Tailscale IP:8765     │ 隧道 │  住宅IP发快手/抖音（风控友好）           │
└─────────────────────────────────────────────┘      └─────────────────────────────────────────┘
```

数据面：**SSH 隧道**（家庭→VPS 公网 22，稳）。Tailscale 作管理面（两边 `tailscale ping` 互通即可）。
> 踩坑记录：家庭联通线路对 Tailscale 直连 UDP 41641 的 wg 数据包做会话级过滤
> （keepalive 110/124B 能过，TCP SYN 加密包 96/128B 被丢），导致 TCP over Tailscale
> 单向不通。ICMP/DISCO 正常 → `tailscale ping` 看似健康，但 TCP 全挂。
> SSH over 公网 22 不受影响，故数据面走 SSH -L 隧道。

## VPS 端部署（Debian 12，/opt/ks-bot）

```bash
git clone https://github.com/carlleilzj/ks-bot.git /opt/ks-bot && cd /opt/ks-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt faster-whisper
timedatectl set-timezone Asia/Shanghai
cp .env.example .env   # 填 AI_*/VISION_*/TELEGRAM_*/REMOTE_API_TOKEN
# .env 关键项：
#   REMOTE_API_TOKEN=<随机hex>       # Bearer token
#   REMOTE_API_BIND=100.x.x.x        # 本机 Tailscale IP（勿用 0.0.0.0，代码会拒绑公网）
#   ASR_PROVIDER=local               # VPS 上跑 faster-whisper small/int8
cp deploy/ksbot-vps.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now ksbot-vps
```

## 家庭端部署（Ubuntu 24.04，~/apps/ks-bot）

```bash
# 代码：本机 `git archive HEAD | ssh ... tar -x` 或 git clone
# 登录态：scp 本机 data/*_state.json → 家庭端 data/（或直接在家庭端 --login 扫码）
mkdir -p ~/apps/ks-bot/{logs,media/{raw,work,final,remote},data}
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
# .env 关键项：
#   REMOTE_API_URL=http://127.0.0.1:8765   # 走 SSH 隧道，不直连
#   REMOTE_API_TOKEN=<与 VPS 相同>
#   TELEGRAM_*：可选（家庭网络可能连不上 TG，失败不影响发布）
# SSH 密钥：ssh-keygen 后把公钥加到 VPS /root/.ssh/authorized_keys
cp deploy/ksbot-tunnel.service deploy/ksbot-publish.service /etc/systemd/system/
# 把 service 里的 PLACEHOLDER_USER / PLACEHOLDER_SSH_PORT 替换为实际值
systemctl daemon-reload
systemctl enable --now ksbot-tunnel
systemctl enable --now ksbot-publish
```

## 验证

```bash
# VPS 端
systemctl status ksbot-vps && curl http://100.x.x.x:8765/api/health
# 家庭端
systemctl status ksbot-tunnel ksbot-publish
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/pending
# TG 审核：给 bot 发链接或等发现层卡片 → 点 ✅ → VPS 处理到 READY → worker 自动发布
```

## 远程发布 API（bot/remote_api.py）

| 端点 | 说明 |
|---|---|
| GET /api/pending | READY 任务 + 平台 job + gate 状态 |
| POST /api/claim | 原子认领（PENDING→PUBLISHING，UPDATE WHERE 防重复） |
| GET /api/file | 下载成品/封面（路径白名单 media/final\|work，带 sha256） |
| POST /api/report | 回报结果（成功/登录失效 SKIPPED/普通失败回 PENDING 重试） |
| GET /api/health | 健康检查 |

安全：Bearer token + 5 次失败封 IP 10 分钟 + 绑定地址白名单（Tailscale CGNAT/私有段）。

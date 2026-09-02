# ks-bot：投链/发现层 → 多平台自动发布（快手 / 抖音 / 小红书 / 视频号）

[![CI](https://github.com/carlleilzj/ks-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/carlleilzj/ks-bot/actions/workflows/ci.yml)

两种采集方式：
1. **被动投链**：Telegram 给 bot 发视频链接，自动处理发布
2. **主动发现**（可选）：发现层周期去 YouTube 搜索/RSS 订阅采集热门动画短视频 → 发 TG 审核卡片 → 你点按钮通过后才进流水线

Telegram 给 bot 发视频链接（Instagram / YouTube / Facebook / TikTok 等），自动：下载 → 转码 → 真人检测 → 语音识别 → AI 按平台生成标题/说明/标签 → 烧录硬字幕 → Playwright 发布到已登录的平台 → Telegram 通知。

```
[TG 投链] → yt-dlp 下载 → ffmpeg 转码/封面 → 真人镜头检测(真人则跳过)
   → ASR 语音识别 → LLM 按平台文案 → 字幕烧录
   → Playwright 发布（每平台独立 job、互不阻塞）→ TG 聚合通知
```

旧版「轮询别人的 IG 账号」已停用（`bot/monitor`、`ig_login.py` 仍保留为遗留代码）。

每条作品是一个 SQLite 状态机任务：`DETECTED → DOWNLOADED → TRANSCODED → TRANSCRIBED → COPYWRITTEN → SUBTITLED → PUBLISHED → NOTIFIED`；
发布阶段拆成 per-platform 的 `publish_jobs`（PENDING → PUBLISHED / FAILED / SKIPPED），单平台失败不影响其他平台，任务失败自动重试 3 次，可随时断点续跑。

## 一、准备

### 1. 本机依赖

```bash
# 注意：最新版 brew 的 ffmpeg 是精简构建（无 libass，烧不了字幕），要用 ffmpeg@7
brew install ffmpeg@7
python3 --version      # 需要 Python 3.11+
```

bot 会自动探测 `ffmpeg@7/6/5`、`ffmpeg-full` 等 Homebrew 路径及 PATH 中的 ffmpeg；
也可在运行前 `export FFMPEG_PATH=/path/to/ffmpeg`（或其 bin 目录）显式指定。

### 2. 安装

```bash
cd ks-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # 下载浏览器（一次性）
# 若想用本地语音识别（ASR_PROVIDER=local）：
pip install -r requirements-local-asr.txt
```

### 3. 准备 Telegram（投链 + 通知）

1. 找 [@BotFather](https://t.me/BotFather) 创建 bot，拿 token 填 `TELEGRAM_BOT_TOKEN`
2. **给你的 bot 发一条消息**（任意内容）
3. 运行 `python -m bot.main --setup`，会自动识别 chat_id 写回 `.env`
   （国内网络不通时配置 `TELEGRAM_API_BASE` 反代或 `TELEGRAM_PROXY`）

日常用法：把视频链接发给 bot。可加平台：`https://... @抖音` 或 `@视频号`。不指定则发到所有启用平台。

### 4. 准备发布平台（一次性，按需）

```bash
python -m bot.main --login kuaishou   # 快手
python -m bot.main --login douyin     # 抖音
python -m bot.main --login xhs        # 小红书
python -m bot.main --login weixin     # 微信视频号
python -m bot.main --login all        # 全部依次登录
```

浏览器会依次打开各平台创作者中心，扫码登录后自动保存登录态（`data/<平台>_state.json`，
快手沿用 `data/ks_state.json`），之后日常运行无需再扫码。登录态失效时 bot 会 Telegram 提醒。

登录完成后，在 `config.yaml` 的 `platforms:` 里把对应平台的 `enabled` 改为 `true` 即可启用；
每个平台的文案（标题字数上限/语气/标签）由 AI 自动适配。

### 5. 配置 AI

`.env` 中三项 `AI_BASE_URL / AI_API_KEY / AI_MODEL` 兼容任意 OpenAI 格式接口
（OpenAI / 智谱 GLM / DeepSeek / 各类中转站均可，换供应商只改这三行）。
语音识别默认走同一接口（`ASR_PROVIDER=api`），也可 `ASR_PROVIDER=local`
用本地 faster-whisper（免费离线）。

## 二、运行

```bash
# ① 试跑（强烈建议第一次先这样）：只处理+生成文案，不发布
python -m bot.main --dry-run --once
#    检查 media/final/ 的成品视频、字幕效果，media/work/*_copy.json 的文案质量

# ② 调试发布流程（显示浏览器窗口，观察每一步操作）
python -m bot.main --headed --once

# ③ 正式运行
python -m bot.main

# 常用命令
python -m bot.main --status          # 查看任务列表
python -m bot.main --retry-failed    # 重置失败任务并续跑
python -m bot.main --abandon-unpublished  # 放弃积压未发（不补发，删对应媒体）
```

发布节奏在 `config.yaml` 控制：每日上限（默认 5）、最小间隔（默认 2 小时）、
发布时间窗口（默认 10:00–22:00，窗口外排队）、字幕样式、可选分区列表。
快手可开 `platforms.kuaishou.spark_task`：发布时自动挂 App 里已收藏的星火「关联变现任务」（失败不阻断发布）。

## 三、开机常驻（launchd）

```bash
# 1. 编辑 deploy/com.ksbot.plist，把两处路径改成你的实际路径
# 2. 安装
cp deploy/com.ksbot.plist ~/Library/LaunchAgents/com.ksbot.plist
launchctl load ~/Library/LaunchAgents/com.ksbot.plist
# 查看日志
tail -f logs/bot.log
# 停止
launchctl unload ~/Library/LaunchAgents/com.ksbot.plist
```

## 四、目录说明

```
data/bot.db         任务状态库          media/raw/    IG 原始视频
data/ks_state.json  快手登录态          media/work/   转码/字幕/文案中间产物
logs/bot.log        运行日志            media/final/  烧录字幕后的发布成品
logs/*.png          发布失败截图
```

## 五、常见问题

| 问题 | 处理 |
| --- | --- |
| 快手登录态失效 | 运行 `--login` 重新扫码；失败任务 `--retry-failed` 续跑 |
| 快手提示发布失败但账号健康 | 小火箭把创作者中心走出国了，见「小火箭 / 代理分流」 |
| 快手页面改版，找不到按钮 | 看 `logs/ks_*.png` 截图，更新 `bot/publish/kuaishou.py` 顶部的 `SELECTORS` |
| 分区选不上 | 对照发布页"选择分类"弹层，更新 `config.yaml` 的 `categories` |
| 星火任务没挂上 | App 星火计划里先「收藏」任务；PC 发布页作者服务能看到再开 `spark_task` |
| 视频没人声 | 自动跳过字幕，文案按画面常识生成 |
| TG 收不到 | 检查 token/chat_id；国内网络配 `TELEGRAM_API_BASE` 反代或 `TELEGRAM_PROXY` |
| 转码/烧字幕报错 | 确认安装了含 libass 的 ffmpeg（`brew install ffmpeg@7`，新版 `brew install ffmpeg` 是精简构建） |
| 中文字幕显示为方块 | 字体不可用会自动回退（PingFang SC → Hiragino Sans GB → Heiti SC）；仍异常时改 `config.yaml` 的 `subtitle.font` 为本机已有字体 |

## 六、小火箭 / 代理分流

发布走的是本机 Playwright Chromium，**必须直连**快手/抖音/小红书/视频号创作者中心。Telegram 可以走代理，两边分开。

代码默认会清掉进程里的 `HTTP_PROXY` 并给 Chromium 加 `--no-proxy-server`。这能挡住「规则模式把代理写进环境变量」的情况。

**增强模式 / TUN / 系统接管全部流量时，代码绕不过去**，必须在小火箭规则里给国内站加 DIRECT：

| 平台 | DIRECT 域名 |
| --- | --- |
| 快手 | `*.kuaishou.com` `*.gifshow.com` `*.kwcdn.com` |
| 抖音 | `*.douyin.com` `*.bytedance.com` `*.byteimg.com` |
| 小红书 | `*.xiaohongshu.com` `*.xhscdn.com` |
| 视频号 | `channels.weixin.qq.com` `*.weixin.qq.com` |
| Telegram | 继续走代理（`TELEGRAM_PROXY` / `TELEGRAM_API_BASE`） |

账号后台显示健康、但自动发布报「发布失败」，优先查分流，而不是先当封号。

只有确实要让发布浏览器走代理时，才在 `.env` 填 `PUBLISH_PROXY`。

## 七、风控与合规建议

- 发布频率已默认保守（每平台每天 5 条、间隔 2h、窗口 10–22 点），不要调得太激进
- 新注册的快手号建议先手动发几条养号，再交给 bot
- 建议优先发布你自有版权的内容；各平台创作者协议通常不允许无头自动化和非原创搬运

## 八、发现层（主动采集 + 人工审核）

发现层让 bot 主动去 YouTube 搜索/RSS 订阅采集热门动画短视频，发 TG 审核卡片，
你点按钮通过后才进流水线。**默认关闭**，在 `config.yaml` 的 `discovery:` 段开启。

### 工作流程

```
DiscoveryScheduler（每小时一轮）
  → YouTubeSearchAdapter / RSSAdapter 采集候选
  → FilterChain 过滤（时长/关键词/热度）
  → 入库 CANDIDATE → 预下载封面 → PENDING_REVIEW
  → TG 发审核卡片（封面+标题+来源+4 个按钮）
  → 你点 [✅ 通过] → DETECTED → 进原流水线
  → 你点 [❌ 丢弃] → SKIPPED
  → 你点 [✏️ 改文案] → 发 edit:<id> 你的标题 → 通过
  → 你点 [🎯 指定平台] → 发 target:<id> 抖音,小红书 → 通过
```

### 配置（`config.yaml`）

```yaml
discovery:
  enabled: true           # 改为 true 启动
  interval_min: 60         # 每轮间隔（分钟）
  limit_per_source: 20    # 每个适配器每轮最多取多少条
  max_pending_review: 10  # 待审核堆积上限，超过暂停发现

  filters:
    min_duration: 5       # 最短时长（秒）
    max_duration: 120     # 最长时长（动画短视频）
    min_score: 1000       # YouTube 播放量阈值
    keyword_whitelist: ["anime", "animation", "动画", "manga", "amv"]
    keyword_blacklist: ["reaction", "recap", "compilation"]
    reject_real_person: false  # 真人检测反向过滤：封面检测到真人直接丢弃（动画号推荐开）

  sources:
    - type: youtube_search      # yt-dlp ytsearch
      queries: ["anime short", "anime clip", "amv short"]
    - type: youtube_rss          # 频道 RSS 增量订阅
      channel_ids: ["UCxxxx"]   # 频道 ID
```

### 适配器类型

| 类型 | 说明 | 稳定性 |
| --- | --- | --- |
| `youtube_search` | yt-dlp `ytsearch{N}:query` 关键词搜索 | ⭐⭐⭐⭐⭐ 最稳 |
| `youtube_rss` | YouTube 频道 RSS `feeds/videos.xml` | ⭐⭐⭐⭐⭐ 不限流，增量 |
| `youtube_playlist` | 订阅播放列表（你 curate 的合集） | ⭐⭐⭐⭐ |

### TG 审核操作

收到审核卡片后：
- 点 **✅ 通过并发布** → 进入流水线
- 点 **❌ 丢弃** → 标记 SKIPPED
- 点 **✏️ 改文案再发** → 然后发消息 `edit:<id> 你的新标题`
- 点 **🎯 指定平台** → 然后发消息 `target:<id> 抖音,小红书`

### 注意事项

- 发现层默认 `enabled: false`，不开启时不影响纯投链使用
- 待审核堆积到 `max_pending_review` 会自动暂停发现，避免 TG 刷屏
- `ytsearch` 高频会被 YouTube 限流，`interval_min` 建议 ≥ 30 分钟
- RSS 适配器拿不到 duration，靠 FilterChain 的时长过滤会放行（只卡其他规则）
- 新增的 `CANDIDATE`/`PENDING_REVIEW` 状态已纳入 `--abandon-unpublished` 和媒体清理范围
- **真人检测反向过滤**（`filters.reject_real_person: true`）：封面下载后用 AI 视觉判断，
  检测到真人镜头直接 SKIPPED（动画号应该全非真人）；检测接口异常时**放行**进审核不误杀，
  由人工把关。每次检测消耗一次 vision API 调用

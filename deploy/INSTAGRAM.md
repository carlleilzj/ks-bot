# Instagram / Facebook Reels 出海线部署说明

**独立于国内发布线**：走 Meta 官方 Graph API，不用 Playwright，不需要登录态文件。

---

## 一、为什么是官方 API

| 维度 | Playwright 网页自动化（国内平台） | Meta Graph API（IG/FB） |
| :--- | :--- | :--- |
| 稳定性 | 页面改版即失效，需持续维护选择器 | 官方契约，长期稳定 |
| 风控 | 扫码/验证码/人脸核验，机房 IP 高危 | 面向云端 SaaS 设计，机房 IP 合规 |
| 依赖 | 需要浏览器 + 登录态 JSON | 纯 HTTP，一个 token 即可 |
| 限制 | 无硬限制（但风控是软限制） | 单账号 24h 最多 100 条 |

结论：IG 这条线**不要**用浏览器自动化，用 API 是降维打击。

---

## 二、前置条件（必须先手工完成，程序无法代劳）

### 1. 账号类型

4 个 IG 账号都必须是 **专业号（Business 或 Creator）**。
转换方式：手机 App → 设置 → 账号类型和工具 → 切换为专业账号。

> 个人号无法使用 Content Publishing API，调用会报
> `The Instagram user does not exist / does not have permission`。

### 2. 创建 Meta 应用并拿 token

1. 打开 <https://developers.facebook.com/apps> → 创建应用 → 选择「其他」→「Business」；
2. 左侧添加产品 → **Instagram** → 「API setup with Instagram login」；
3. 在「Generate access tokens」里，用**目标 IG 账号**登录授权；
4. 授权后页面会直接给出两样东西，务必记下：
   - **Instagram account ID**（一串 17 位数字，形如 `17841400000000000`）；
   - **Access token**（形如 `IGAA...`，这是 60 天有效的 long-lived token）；
5. 需要的权限：`instagram_business_basic` + `instagram_business_content_publish`。

> **一个 IG 账号 = 一套 token + ID**。4 个账号要重复 4 次，
> 每次用对应的账号登录授权（别在同一个浏览器窗口里换来换去，
> 先用无痕窗口分别登录授权最稳）。

### 3. Token 续期

long-lived token 有效期 60 天，会过期。续期端点：

```
GET https://graph.instagram.com/refresh_access_token
    ?grant_type=ig_refresh_token
    &access_token=<当前token>
```

返回一个新的 60 天 token。建议每隔 45 天跑一次并更新 `.env`。
（本项目的 `--ig-check` 会在 token 失效时明确报错，不会静默失败。）

---

## 三、配置

### 1. `.env`

```bash
# 每个账号一套（KEY 与 config.yaml 的 platforms 名一致，全大写）
IG_TOKEN_IG_PETS=IGAA...
IG_USERID_IG_PETS=17841400000000001

IG_TOKEN_IG_SATISFYING=IGAA...
IG_USERID_IG_SATISFYING=17841400000000002

IG_TOKEN_IG_ANIM=IGAA...
IG_USERID_IG_ANIM=17841400000000003

IG_TOKEN_IG_HOME=IGAA...
IG_USERID_IG_HOME=17841400000000004

# 媒体公网地址（Meta 从这里拉视频）
IG_MEDIA_BASE_URL=https://media.your-domain.com

# 媒体 URL 签名密钥（不给则回退用 REMOTE_API_TOKEN）
IG_MEDIA_SECRET=<随机32位hex>
```

### 2. `config.yaml`

4 个 IG 平台已预置在 `platforms:` 段，都是 `enabled: false`。
配好 `.env` 并跑通 `--ig-check` 后再逐个改 `true`。

```yaml
  ig_pets:
    enabled: true
    default: false      # 必须 false：只有链接里写 @ig萌宠 才发
    share_to_feed: true
    is_ai_generated: false
```

> **`default: false` 是硬要求**：IG 的日限与国内平台完全独立，
> 若设成 true，任何没写平台名的链接都会同时发到 IG，
> 与「国内素材发国内、出海素材发出海」的意图冲突。

---

## 四、媒体托管（Meta 必须能访问到视频）

Meta 的 Reels API **不接受文件上传**，只接受 `video_url`，
并且是 **Meta 自己的服务器去 cURL 这个 URL**。所以视频必须先公网可访问。

本项目的做法（`bot/publish/media_upload.py`）：

1. 协调端启动一个只读媒体服务（默认 `127.0.0.1:8766`）；
2. 视频路径带 **HMAC 签名 + 过期时间**（默认 2 小时），
   不能枚举、不能篡改、过期即失效；
3. 只允许访问 `media/final/` 目录内的文件（路径穿越直接拒绝）；
4. nginx 把 `/ig-media/` 前缀反代进来。

### nginx 配置（协调端）

```nginx
server {
    listen 443 ssl http2;
    server_name media.your-domain.com;

    ssl_certificate     /root/.acme.sh/media.your-domain.com_ecc/fullchain.cer;
    ssl_certificate_key /root/.acme.sh/media.your-domain.com_ecc/media.your-domain.com.key;

    # 只暴露媒体路径，其余一律 404
    location /ig-media/ {
        proxy_pass http://127.0.0.1:8766;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        # 视频文件可能上百 MB，别让 nginx 缓冲拖慢
        proxy_buffering off;
        proxy_request_buffering off;
        client_max_body_size 0;
    }

    location / { return 404; }
}
```

申请证书：

```bash
~/.acme.sh/acme.sh --issue -d media.your-domain.com --nginx
```

### 自检

```bash
# 1. 媒体服务是否在听
curl -sI http://127.0.0.1:8766/ig-media/badtoken/x.mp4   # 期望 403

# 2. 公网链路（用真实签名 URL 测，见下）
```

真实签名 URL 的生成方式（在协调端执行）：

```bash
python -c "
from pathlib import Path
from bot.publish.media_upload import build_public_url
from bot.config import FINAL_DIR
v = next(FINAL_DIR.glob('*_final.mp4'))
print(build_public_url(v))
"
```

然后用 `curl -sI <输出的URL>` 确认返回 `200` 且 `Content-Type: video/mp4`。
**这一步必须通过**，否则发布会失败在「容器状态 ERROR」。

---

## 五、发布流程（程序内部）

```
TG 投链 → 下载/转码/AI英文文案（ig_* profile）
   ↓
协调端 /api/media-url 生成签名公网 URL
   ↓
发布端（阿里云）：POST /<IG_ID>/media（media_type=REELS, video_url, caption）
   ↓
轮询 GET /<CONTAINER_ID>?fields=status_code 直到 FINISHED（最长 5 分钟）
   ↓
POST /<IG_ID>/media_publish（creation_id）
   ↓
GET /<MEDIA_ID>?fields=permalink → TG 回报作品链接
```

关键安全点（都有单测锁定，见 `tests/test_instagram_publish.py`）：

- 容器状态不是 `FINISHED` **绝不**调 media_publish（否则发出坏内容）；
- 24h 配额 ≥100 时**直接拒绝**，不浪费一次 API 调用；
- 凭据缺失抛 `LoginExpired`，job 退回 PENDING 等人工补，不会烧掉队列。

---

## 六、使用

```bash
# 1. 配置自检（不发视频）
python -m bot.main --ig-check

# 2. 手工发一条测试（TG 投链时指定账号）
#    把国内素材链接发给 bot，加 @ig萌宠 即只发该 IG 账号
#    例：https://v.douyin.com/xxxxx @ig萌宠

# 3. 看日志
journalctl -u ks-bot.service -f | grep -i instagram
journalctl -u ksbot-publish.service -f | grep -i instagram
```

---

## 七、常见错误对照

| 报错关键字 | 原因 | 处理 |
| :--- | :--- | :--- |
| `IG_TOKEN_xxx 未配置` | `.env` 缺凭据 | 补齐 token 与 user id |
| `凭据校验失败` / `code=190` | token 过期或失效 | 用 refresh 端点换新 token |
| `does not have permission` | 账号不是专业号 / 缺权限 | 切换专业号，重新授权 |
| 容器 `status=ERROR` | 视频 URL 打不开 / 格式不支持 | 先 `curl -I` 验证媒体 URL；确认是 H.264 MP4 |
| `quota_usage >= 100` | 24h 达到上限 | 等次日，或分流到其他账号 |
| 容器一直 `IN_PROGRESS` | 视频过大或 Meta 处理排队 | 降低码率/时长；`wait_container_ready` 最长等 5 分钟 |

---

## 八、前置链路：国内素材怎么进来

出海任务的 `target_platforms` 里只要有 `ig_*`，整条流水线就走**出海分支**，
与国内分支的差异全部自动生效，不需要额外的开关。

### 1. 投链方式

直接把国内素材的链接发给 TG bot，末尾加 `@ig*`：

```
https://v.douyin.com/iABC123/ @ig萌宠
https://www.xiaohongshu.com/explore/6789abc @ig家居
```

也支持**直接粘贴整段分享文案**（国内 App 导出的那种），程序会自动抠出 URL：

```
7.89 abc:/ 复制打开抖音，看看【某某的作品】... https://v.douyin.com/iABC123/ 复制此链接 @ig解压
```

短链会自动展开（`v.douyin.com` / `xhslink.com` / `b23.tv`），
然后交给 yt-dlp 解析元数据 + 下载无水印原片。

### 2. 画面清洗（`bot/media/export.py`）

| 处理 | 国内分支 | 出海分支 |
| :--- | :--- | :--- |
| 中文硬字幕 | 生成并烧录 | **裁掉底部字幕区**（比例按账号 8%~12%，带随机抖动） |
| 裁后处理 | — | **放大回原分辨率**，保持 9:16 满屏 |
| 音频 | 保留 | 按账号：解压/萌宠/家居**保声**，动画**剥音轨** |
| 水印 | delogo（按需） | delogo（默认 `corner_pair` 双点位） |
| 二创变换 | 可选 | 可选（同一套 `transform` 配置） |

**账号配方**（`ExportProfile.for_account`）：

| 账号 | 裁字幕 | 音轨 | 原因 |
| :--- | :--- | :--- | :--- |
| `ig_satisfying` | 8% | **保留** | ASMR 原声就是内容本体，剥掉等于毁片 |
| `ig_pets` | 10% | 保留 | 叫声/环境声有信息量 |
| `ig_anim` | 12% | **剥离** | 3D/AI 动画多配中文旁白，海外无意义且暴露搬运 |
| `ig_home` | 9% | 保留 | 轻音乐是氛围的一部分 |

### 3. 字幕阶段

出海任务**跳过中文字幕烧录**（`step_subtitle` 直接 copy）。
老外看不懂中文，烧上去只会显得廉价、拉低完播。

### 4. 成品校验

`verify_export` 会在日志里告警（不阻断）：

- 输出仍是横屏 → IG Reels 会裁掉两侧主体；
- 高度超过 1080p 等效 → 会被平台二次压缩；
- 时长 <3s 或 >90s → 前者难进推荐，后者掉完播。

---

## 九、上线前的两次冒烟

```bash
# 1. 发布链路（mock Meta，不消耗真实配额）
python tools/smoke_ig_pipeline.py

# 2. 画面清洗（真实 ffmpeg，验证滤镜链能出片）
python tools/smoke_export_video.py
```

两个脚本都通过，才算真正可以开 `enabled: true`。

---

---

## 十、与国内线的隔离原则

- **日限独立**：`platforms.ig_*.daily_limit` 单独计，不占快手的 6 条；
- **文案独立**：`ig_*` profile 走全英文 prompt，不套中文禁用词表；
- **默认队列隔离**：全部 `default: false`，只有显式 `@ig*` 才发；
- **失败隔离**：IG 失败不影响国内 job（per-platform job 状态机天然隔离）。

# 自建 Telegram Bot API Server

## 为什么值得做

| 维度 | 官方 api.telegram.org | 自建 + `--local` |
| :--- | :--- | :--- |
| 单文件下载上限 | **20 MB** | **2000 MB** |
| 取文件方式 | HTTP 下载（走公网、走代理） | `getFile` 直接返回**本机绝对路径** |
| 传到本地耗时 | 20MB ≈ 数十秒（视网络） | **硬链接，0 秒 0 传输** |
| 稳定性 | 受国内网络/代理影响 | 本机回环，不受影响 |

对我们这条流水线来说，**第二和第四点比"解除限制"更有价值**：
视频本来就落在服务器上，官方通道还要再下载一遍纯属浪费。

---

## 一、申请 api_id / api_hash（必须人工，一次性）

1. 浏览器打开 <https://my.telegram.org/apps>
2. 用**你的手机号**登录（不是 bot token，是你本人的 TG 账号）
3. 填写应用信息（名字随便，如 `ks-bot-pipeline`，平台选 Other）
4. 提交后拿到：
   - `api_id`：一串数字，如 `12345678`
   - `api_hash`：32 位十六进制

> ⚠️ 这两个值等同于你的开发者身份，**不要泄露**。
> 一个账号可以建多个应用，误删了重新建即可。

---

## 二、部署

在协调端（`gc-jp`，也就是跑 `ks-bot.service` 的那台）执行：

```bash
cd /opt/ks-bot
bash deploy/setup-bot-api.sh <api_id> <api_hash>
```

脚本会自动：
- 创建 `/opt/tg-bot-api/data` 数据目录
- 用 Docker 启动官方镜像 `aiogram/telegram-bot-api`
- 只监听 `127.0.0.1:8081`（不对外暴露，无需 TLS）
- 等待就绪并打印状态

**关键参数说明**：

| 参数 | 作用 |
| :--- | :--- |
| `--local`（脚本里用 `TELEGRAM_LOCAL=1`） | **解除限制的核心开关**：允许无限下载、2000MB 上传、getFile 返回本机路径 |
| `-p 127.0.0.1:8081` | 只绑回环，外部访问不到 |
| `-v .../data:/var/lib/telegram-bot-api` | 持久化 bot 会话与文件缓存 |

---

## 三、切换 bot 到自建通道（关键，容易漏）

### 3.1 改 `.env`

```bash
TELEGRAM_API_BASE=http://127.0.0.1:8081
```

### 3.2 把 bot 从官方服务器注销

**这一步不能跳过**。官方服务器和自建服务器不能同时为同一个 bot 收消息，
不同步的话会丢 update（消息发出去 bot 没反应）。

```bash
# 用你的 token 调一次 logOut
curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/logOut"
```

> 之后所有请求都要发到自建地址。若想回滚，同样用**自建地址**调一次 logOut，
> 再把 `TELEGRAM_API_BASE` 改回去。

### 3.3 重启服务

```bash
systemctl restart ks-bot.service
```

---

## 四、验证

```bash
# 1. 服务在跑
docker ps --filter name=tg-bot-api

# 2. bot 能连上（发一条消息给 bot，看日志）
journalctl -u ks-bot.service -f | grep -i telegram

# 3. 发一个 >20MB 的视频给 bot
#    回执里如果显示「收到本地视频文件（XX MB）」且能入库 = 成功
```

**如何确认走的是 local 模式**：日志里应出现
`TG 本地文件已硬链接：xxx.mp4（xx.x MB）`，而不是下载进度。

---

## 五、代码如何适配（已实现）

`bot/notify/telegram.py` 会自动识别通道：

```python
def is_local_bot_api(s):        # base 里不含 api.telegram.org 即为自建
def max_download_mb(s):         # 官方 20 / 自建 2000
def download_file(...):
    # 自建 + path 是绝对路径 → os.link 硬链接（失败退回复制）
    # 否则 → 走 HTTP /file/bot<token>/<path> 下载
```

因此**不需要改任何业务代码**，只改 `.env` 一行即可，且可随时回滚。

---

## 六、运维

```bash
# 查看日志
docker logs -f tg-bot-api

# 重启
docker restart tg-bot-api

# 升级镜像
docker pull aiogram/telegram-bot-api:latest
docker rm -f tg-bot-api && bash deploy/setup-bot-api.sh <id> <hash>

# 磁盘占用（缓存的媒体文件）
du -sh /opt/tg-bot-api/data
```

**磁盘清理**：`--local` 模式下 bot 上传的文件会存在 `/opt/tg-bot-api/data`，
我们的流程是「取走即用」，可以在 crontab 里加定期清理：

```bash
# 清理 3 天前的 TG 缓存
0 4 * * * find /opt/tg-bot-api/data -type f -mtime +3 -delete
```

---

## 七、常见问题

| 现象 | 原因 | 处理 |
| :--- | :--- | :--- |
| bot 完全不响应新消息 | 没调 `logOut`，与官方服务器双登录 | 用官方地址调一次 logOut，再重启服务 |
| 报 `Unauthorized` | api_id/api_hash 填错或应用被删 | 去 my.telegram.org 核对 |
| 大文件仍被拒 | `.env` 没改 / 没重启服务 | 确认 `TELEGRAM_API_BASE` 指向 8081 |
| `getFile` 返回相对路径 | 没开 `--local` | 检查容器有 `TELEGRAM_LOCAL=1` |
| 磁盘涨得快 | TG 缓存未清理 | 加 crontab 定期清理 |
| 端口被占 | 8081 已被别的服务用 | 改 `--http-port` 与 `.env` 同步 |

---

## 八、成本

- **磁盘**：媒体缓存，按需清理即可，通常 <5GB
- **内存**：约 50~100MB
- **CPU**：几乎不占（只在收发时工作）

协调端 12G 内存、79G 可用空间，完全够用。

---

## 九、要不要做？

**建议先不做**，除非你满足以下任一条件：

- 经常发 **>20MB** 的素材（4K 原片、长视频）；
- 发文件时明显感觉**下载慢**（走代理更慢）；
- 需要**秒级**投递（现在 20MB 要等几十秒）。

如果只是偶尔发小视频，官方通道的 20MB 够用，不值得为此多维护一个容器。

**收益/成本比**：真正干活的是「硬链接零拷贝」——当你每天投十几条时，
省下的等待时间才明显。建议等这条线跑顺、日均投递量上来了再上。

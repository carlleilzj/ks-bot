"""Telegram 消息监听：长轮询 getUpdates，收到视频链接后入库。

独立线程运行，与主循环共享同一个 Database 实例。
收到链接 → 规范化 → 查重 → yt-dlp 提取元数据 → 插入 DB（DETECTED）→ 回复确认。
"""

from __future__ import annotations

import logging
import re
import threading
import time

import httpx

from ..config import RAW_DIR, Settings
from ..db import Database, State
from ..notify import telegram
from ..publish import enabled_publishers
from .downloader import extract_meta, parse_url

log = logging.getLogger(__name__)

# URL 正则：匹配消息里的 http(s) 链接。
# 中文用户常写「看这个 https://.../reel/XXX，很好笑」，若把「，很好笑」一起吃进去，
# shortcode 会带非法字符、白耗 8 次重试 → 排除中文标点与 , !。
#
# 但绝不能排除 ? : & = # 这些「URL 内部合法字符」：查询参数型链接会被截断
# （youtube.com/watch?v=ABC → /watch，facebook.com/watch/?v=123 → /watch/），
# yt-dlp 必然解析失败。链接末尾零散的英文标点另行剥离。
_URL_RE = re.compile(
    r"https?://[^\s<>'\"，。！？、；）】」》”’,!]+",
    re.IGNORECASE,
)
_URL_TRAILING_PUNCT = ",.!?;:)'\"”’"

# getUpdates long-poll 超时（秒）
_POLL_TIMEOUT = 30

# 状态中文映射（给用户看的回复）
_STATE_LABELS = {
    State.DETECTED: "待下载",
    State.DOWNLOADED: "已下载，待转码",
    State.TRANSCODED: "已转码，待转录",
    State.TRANSCRIBED: "已转录，待生成文案",
    State.COPYWRITTEN: "文案已生成，待加字幕",
    State.SUBTITLED: "已就绪，等待发布窗口",
    State.READY: "已就绪，等待发布",
    State.PUBLISHED: "已发布",
    State.NOTIFIED: "已完成通知",
    State.FAILED: "失败",
    State.SKIPPED: "已跳过",
    State.BASELINE: "基线记录",
}


class TelegramListener(threading.Thread):
    """长轮询监听 TG 消息，收到视频链接后插入 DB。"""

    daemon = True

    def __init__(self, s: Settings, db: Database, wakeup: threading.Event | None = None):
        super().__init__(name="tg-listener")
        self.s = s
        self.db = db
        self._offset = 0
        self._stop = threading.Event()
        self._wakeup = wakeup

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not self.s.telegram_bot_token:
            log.warning("TG 监听未启动：TELEGRAM_BOT_TOKEN 未配置")
            return
        log.info("TG 消息监听线程已启动，等待接收视频链接…")
        # 注册命令菜单：TG 客户端输入 / 弹出快捷列表，点选即发
        if telegram.set_commands(self.s, [
            ("login", "重新扫码登录（用法：/login weixin）"),
            ("help", "使用帮助"),
            ("start", "使用帮助"),
        ]):
            log.info("TG 命令菜单已注册：/login /help /start")
        telegram.notify_info(self.s, "🎧 投链监听已启动\n直接发视频链接给我即可（IG/YouTube/Facebook/TikTok 等）")
        # 解析重试放独立线程：extract_meta 是同步网络请求（单条可达几十秒），
        # 若在 poll 循环里跑，期间按钮回调全部排队，等处理时 answerCallbackQuery
        # 已过期（TG 约 15s 上限）→ 按钮转圈半天、toast 丢失（2026-10-25 实测）。
        retry_t = threading.Thread(target=self._retry_loop, name="tg-resolve-retry",
                                   daemon=True)
        retry_t.start()
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                log.exception("TG 监听异常，30s 后重试：%s", e)
                time.sleep(30)

    def _retry_loop(self) -> None:
        """后台排空到期的解析重试项（慢网络操作，不阻塞按钮回调）。"""
        while not self._stop.is_set():
            try:
                self._retry_due_resolves()
            except Exception as e:
                log.debug("解析重试处理异常：%s", e)
            self._stop.wait(20)

    def _retry_due_resolves(self, limit: int = 5) -> int:
        """排空到期的解析重试项。成功入流水线，失败按指数退避再排。"""
        rows = self.db.due_resolves(limit=limit)
        if not rows:
            return 0
        done = 0
        for row in rows:
            clean_url = row["clean_url"]
            tp = [x for x in (row.get("target_platforms") or "").split(",") if x] or None
            tp, tp_disabled = _split_disabled_targets(self.s, tp)
            if tp_disabled and not tp:
                log.warning("重试 #%d 指定平台 %s 已停用，放弃入库",
                            row["id"], tp_disabled)
                self.db.resolve_done(row["id"])
                continue
            log.info("\U0001f504 重试解析 #%d（第 %d 次）：%s",
                     row["id"], row["attempts"] + 1, clean_url)

            try:
                if self.db.find_by_source_url(clean_url):
                    self.db.resolve_done(row["id"])
                    continue
            except Exception:
                pass

            try:
                meta = extract_meta(clean_url)
            except ValueError as e:
                giveup, delay = self.db.resolve_fail(row["id"], str(e)[:200])
                if giveup:
                    telegram.send_text(self.s, (
                        "\u274c 解析最终失败（已重试 %d 次）\n链接：%s\n原因：%s"
                        % (row["attempts"] + 1, clean_url, str(e)[:150])
                    ))
                else:
                    log.info("重试 #%d 仍失败，%d 秒后再试", row["id"], delay)
                continue
            except Exception as e:
                self.db.resolve_fail(row["id"], ("未预期异常：%s" % e)[:200])
                continue

            try:
                if self.db.find_by_source_url(meta.source_url):
                    self.db.resolve_done(row["id"])
                    continue
                tid = self.db.insert_video(meta, target_platforms=tp)
                self.db.resolve_done(row["id"])
                if tid is None:
                    continue
                done += 1
                platform_cn = _PLATFORM_CN.get(meta.platform, meta.platform)
                pub_names = ("、".join(_PUBLISH_PLATFORM_CN.get(x, x) for x in tp)
                             if tp else "所有启用平台")
                telegram.send_text(self.s, (
                    "\u2705 重试解析成功（第 %d 次）\n[%s] %s\n标识：%s\n发布到：%s\n\n"
                    "流水线：下载 \u2192 转码 \u2192 转录 \u2192 文案 \u2192 字幕 \u2192 发布"
                    % (row["attempts"] + 1, platform_cn, meta.username or "未知",
                       meta.shortcode, pub_names)
                ))
                log.info("\u2705 重试入库 [%s] tid=%d", meta.shortcode, tid)
                if self._wakeup is not None:
                    self._wakeup.set()
            except Exception as e:
                log.exception("重试解析后入库失败：%s", e)
                self.db.resolve_fail(row["id"], ("入库失败：%s" % e)[:200])
        return done

    def _poll_once(self) -> None:

        """调一次 getUpdates（long-poll），处理返回的所有更新。"""
        url = f"{self.s.telegram_api_base}/bot{s_telegram_token(self.s)}/getUpdates"
        params = {"offset": self._offset, "timeout": _POLL_TIMEOUT}
        client_kw: dict = {"timeout": _POLL_TIMEOUT + 10}
        if self.s.telegram_proxy:
            client_kw["proxy"] = self.s.telegram_proxy

        try:
            with httpx.Client(**client_kw) as client:
                resp = client.get(url, params=params)
        except Exception as e:
            log.debug("getUpdates 请求失败：%s", e)
            time.sleep(5)
            return

        if resp.status_code != 200:
            log.error("getUpdates 返回 HTTP %d", resp.status_code)
            time.sleep(5)
            return

        data = resp.json()
        if not data.get("ok"):
            log.error("getUpdates 返回错误：%s", str(data)[:200])
            time.sleep(5)
            return

        # 同批更新里按钮回调优先：投链消息的处理含 extract_meta（秒级网络
        # 请求），排前面会把同批的回调拖过 answerCallbackQuery 的 ~15s 上限
        for upd in sorted(data.get("result", []),
                          key=lambda u: 0 if u.get("callback_query") else 1):
            self._offset = upd["update_id"] + 1
            self._handle_update(upd)

    def _handle_update(self, upd: dict) -> None:
        """处理单条 update（callback_query 或 message）。"""
        # 优先处理内联按钮回调
        cq = upd.get("callback_query")
        if cq:
            self._handle_callback(cq)
            return

        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            return
        chat_id = str(msg.get("chat", {}).get("id", ""))

        # 只处理配置的 chat_id（安全：忽略其他人发来的消息）
        if self.s.telegram_chat_id and chat_id != self.s.telegram_chat_id:
            log.debug("忽略非配置 chat_id 的消息：%s", chat_id)
            return

        # 直接发视频/文件 = 本地文件投链（绕过 yt-dlp，任何来源的素材都能进流水线）
        video_obj = msg.get("video") or msg.get("animation")
        doc_obj = msg.get("document") or {}
        if video_obj or doc_obj:
            self._handle_local_file(msg, video_obj, doc_obj)
            return

        text = msg.get("text") or ""
        if not text:
            return

        # /login 指令：远程触发平台扫码重登（跑在协调端，二维码由发布端 TG 推送）
        t = text.strip()
        if t.lower().startswith("/login"):
            self._handle_login_cmd(t)
            return

        # 提取 URL（末尾英文标点单独剥离，见 _URL_RE 注释）
        urls = [u.rstrip(_URL_TRAILING_PUNCT) for u in _URL_RE.findall(text)]
        urls = [u for u in urls if u]
        if not urls:
            # 非 URL 消息：只响应明确的指令
            t_low = t.lower()
            if t_low in ("/help", "/start", "帮助"):
                telegram.send_text(
                    self.s,
                    "📬 发视频链接或直接发视频文件给我\n"
                    "支持链接：Instagram / YouTube / Facebook / TikTok / 抖音 / 小红书 / B站 等\n"
                    "也支持直接发 mp4 文件（yt-dlp 不支持的来源用这个）\n"
                    "收到后会自动转码 → 生成文案 → 加字幕 → 发布\n"
                    "重复内容会自动判定，不会重复处理\n\n"
                    "💡 指定发布平台：链接后加平台名，如\n"
                    "  https://... @抖音\n"
                    "  https://... @douyin,xhs\n"
                    "不指定则发到所有启用的平台\n\n"
                    "📎 发本地文件时，平台标记写在**文件说明（caption）**里，如：\n"
                    "  发视频 + 说明填「@ig萌宠」\n\n"
                    "🌍 出海发布（Instagram 矩阵号，自动裁中文字幕+英文文案）：\n"
                    "  https://... @ig萌宠   → 搞笑萌宠号\n"
                    "  https://... @ig解压   → 视觉解压号（保留原声 ASMR）\n"
                    "  https://... @ig动画   → 3D 动画号\n"
                    "  https://... @ig家居   → 家居好物号\n"
                    "  可直接粘贴抖音/小红书分享文案，会自动抠出链接\n\n"
                    "🔑 /login —— 平台扫码重登（不带参数弹菜单）\n"
                    "   /login weixin / kuaishou / douyin / toutiao\n"
                    "   中文别名也行：/login 快手、/login 快手2\n",
                )
            return

        # 解析发布平台指令（链接之外的文本）
        target_platforms, unknown = _parse_target_platforms(text)
        # 停用平台是硬性的：显式指定不能绕过 config 开关（2026-10-10，
        # 视频号 IP 风控期间被 @视频号 指定绕过，每条失败刷屏）
        target_platforms, disabled = _split_disabled_targets(self.s, target_platforms)
        if disabled and not target_platforms:
            names = "、".join(_PUBLISH_PLATFORM_CN.get(p, p) for p in disabled)
            telegram.send_text(
                self.s,
                f"⛔ {names} 已停用，本条未入库\n"
                f"如需发到其他平台，请重发链接并指定（如：链接 @快手）",
            )
            return
        if disabled:
            names = "、".join(_PUBLISH_PLATFORM_CN.get(p, p) for p in disabled)
            telegram.send_text(self.s, f"⚠️ {names} 已停用，本条将跳过它继续")

        for raw_url in urls:
            try:
                self._handle_url(raw_url, target_platforms, unknown)
            except Exception as e:
                log.exception("处理链接失败：%s", raw_url)
                telegram.send_text(self.s, f"❌ 处理链接失败：{str(e)[:200]}\n链接：{raw_url}")

    # ---------- 本地文件投链 ----------

    def _handle_local_file(self, msg: dict, video_obj: dict | None,
                           doc_obj: dict) -> None:
        """处理用户直接发来的视频/文件：下载 → 入库（跳过解析与下载阶段）。

        为什么需要这条路径：
        yt-dlp 不支持的来源（Threads、私域链接、本机录屏、AI 生成素材等）
        原先完全进不了流水线。本地文件投链让「任何来源的 mp4」都能走完
        转码 → AI 文案 → 清洗 → 发布，下游逻辑完全复用。
        """
        media = video_obj or doc_obj

        # 文件类型校验：只收视频（避免误发图片/文档把队列搞乱）
        if video_obj:
            mime = (video_obj.get("mime_type") or "video/mp4")
            file_name = video_obj.get("file_name") or ""
        else:
            mime = doc_obj.get("mime_type") or ""
            file_name = doc_obj.get("file_name") or ""
        if not (mime.startswith("video/") or mime == "application/octet-stream"):
            telegram.send_text(
                self.s,
                f"⚠️ 只接受视频文件（收到 {mime or '未知类型'}）\n"
                f"请直接发送 mp4 视频，或发链接让我下载。",
            )
            return

        file_id = media.get("file_id") or ""
        if not file_id:
            return

        file_size = int(media.get("file_size") or 0)
        size_mb = file_size / 1048576
        limit_mb = telegram.max_download_mb(self.s)
        if file_size and size_mb > limit_mb:
            hint = ("先压缩/剪辑到上限以内再发"
                    if limit_mb <= telegram.TG_MAX_DOWNLOAD_MB_OFFICIAL
                    else "文件过大，建议先压缩")
            telegram.send_text(
                self.s,
                f"⚠️ 文件 {size_mb:.1f} MB 超过当前通道上限（{limit_mb} MB）\n\n"
                f"{hint}\n"
                f"或改用链接投递（让服务器自己下载）",
            )
            return

        # 解析发布平台：文件名或 caption 里都能带 @标记
        caption = msg.get("caption") or ""
        hint_text = f"{caption} {file_name}"
        target_platforms, unknown = _parse_target_platforms(hint_text)
        target_platforms, disabled = _split_disabled_targets(self.s, target_platforms)
        if disabled and not target_platforms:
            names = "、".join(_PUBLISH_PLATFORM_CN.get(p, p) for p in disabled)
            telegram.send_text(self.s, f"⛔ {names} 已停用，本条未入库")
            return

        # 生成唯一标识：文件唯一 id + 时间戳，避免同一文件重复投被当成同一条
        unique_key = (media.get("file_unique_id") or file_id)[:32]
        shortcode = f"local_{unique_key}"

        # 查重：同一文件重复发送（同一 shortcode）直接提示
        existing = self.db.find_by_source_url(f"local://{unique_key}")
        if existing:
            state = existing.get("state", "")
            label = _STATE_LABELS.get(state, state)
            telegram.send_text(
                self.s,
                f"⏳ 这个文件已经投过了\n标识：{existing.get('shortcode','?')}\n状态：{label}",
            )
            return

        telegram.send_text(
            self.s,
            f"📥 收到本地视频文件{'（' + file_name + '）' if file_name else ''}\n"
            f"正在下载（{size_mb:.1f} MB）…",
        )

        raw_path = RAW_DIR / f"{shortcode}.mp4"
        if telegram.download_file(self.s, file_id, raw_path) is None:
            telegram.send_text(self.s, "❌ 下载失败，请重试或改用链接投递")
            return

        # 校验确实是视频（有宽高/时长），避免把伪装成 mp4 的文件塞进流水线
        try:
            from ..media import ffmpeg
            info = ffmpeg.video_info(raw_path)
        except Exception as e:
            raw_path.unlink(missing_ok=True)
            telegram.send_text(self.s, f"❌ 文件不是有效视频：{str(e)[:150]}")
            return

        if not info.get("duration") or info["duration"] <= 0:
            raw_path.unlink(missing_ok=True)
            telegram.send_text(self.s, "❌ 视频时长为 0，文件可能损坏")
            return

        # 入库：state 直接 DOWNLOADED（文件已在本地，跳过下载阶段）
        from .downloader import VideoMeta
        meta = VideoMeta(
            source_url=f"local://{unique_key}",
            platform="local",
            video_id=unique_key,
            shortcode=shortcode,
            username=msg.get("from", {}).get("username") or "local",
            title=file_name or shortcode,
            caption=caption,
            thumbnail_url="",
            duration=float(info.get("duration") or 0),
            permalink="",
        )
        tid = self.db.insert_video(meta, target_platforms=target_platforms,
                                   state=State.DOWNLOADED)
        if tid is None:
            raw_path.unlink(missing_ok=True)
            telegram.send_text(self.s, "⚠️ 该文件已入库（可能刚被处理）")
            return
        self.db.update(tid, raw_path=str(raw_path))

        if target_platforms:
            pub_names = "、".join(_PUBLISH_PLATFORM_CN.get(p, p) for p in target_platforms)
            pub_line = f"发布到：{pub_names}"
        else:
            from ..publish import default_publishers
            def_names = "、".join(_PUBLISH_PLATFORM_CN.get(p.name, p.name)
                                  for p in default_publishers(self.s))
            pub_line = f"发布到：{def_names or '（无默认平台）'}"

        warn_line = f"\n⚠️ 未识别的平台名：{', '.join(unknown)}" if unknown else ""
        telegram.send_text(
            self.s,
            f"✅ 本地视频已入库\n"
            f"标识：{shortcode}\n"
            f"分辨率：{info['width']}x{info['height']}\n"
            f"时长：{int(info.get('duration') or 0)}秒\n"
            f"{pub_line}{warn_line}\n\n"
            f"流水线：转码 → 转录 → 文案 → 字幕 → 发布（已跳过下载）",
        )
        log.info("📥 本地文件入库 [%s] tid=%d targets=%s",
                 shortcode, tid, target_platforms or "all")
        if self._wakeup is not None:
            self._wakeup.set()

    # ---------- callback_query 处理（登录菜单按钮） ----------

    def _handle_callback(self, cq: dict) -> None:
        """处理内联按钮回调。callback_data 格式：login:<platform>。"""
        data = cq.get("data") or ""
        action, _, platform = data.partition(":")
        cq_id = cq.get("id", "")
        if action != "login":
            telegram.answer_callback(self.s, cq_id, "不支持的操作")
            return
        key = platform.strip().lower()
        if key not in ("weixin", "toutiao", "kuaishou", "kuaishou2", "douyin", "xhs"):
            telegram.answer_callback(self.s, cq_id, "不支持的平台")
            return
        telegram.answer_callback(self.s, cq_id, f"已触发 {key} 扫码登录")
        self._handle_login_cmd(f"/login {key}")

    def _handle_login_cmd(self, text: str) -> None:
        """远程触发平台扫码重登。二维码由发布端脚本推到 TG。

        协调端无法直接跑发布端浏览器（state 文件在阿里云），这里通过
        remote_api 转发指令，由发布端执行 --login-qr 并推码。
        目前支持 weixin / toutiao。
        """
        parts = text.split()
        platform = parts[1] if len(parts) > 1 else ""
        aliases = {
            "weixin": "weixin", "微信": "weixin", "视频号": "weixin", "wx": "weixin",
            "toutiao": "toutiao", "头条": "toutiao", "tt": "toutiao",
            "kuaishou": "kuaishou", "快手": "kuaishou", "ks": "kuaishou",
            "douyin": "douyin", "抖音": "douyin", "dy": "douyin",
        }
        key = aliases.get(platform.lower())
        if not key:
            # 没带平台名：弹按钮菜单，点选即登录
            keyboard = {"inline_keyboard": [
                [{"text": "微信视频号", "callback_data": "login:weixin"},
                 {"text": "快手", "callback_data": "login:kuaishou"}],
                [{"text": "快手2号", "callback_data": "login:kuaishou2"},
                 {"text": "抖音", "callback_data": "login:douyin"}],
                [{"text": "今日头条", "callback_data": "login:toutiao"}],
            ]}
            telegram.send_text(
                self.s, "🔑 点选要重新登录的平台：", reply_markup=keyboard)
            return
        try:
            from ..remote_api import trigger_remote_login
            ok = trigger_remote_login(self.s, key)
            if ok:
                telegram.send_text(
                    self.s, f"🔑 {key} 扫码登录已触发，二维码即将推送到 TG")
            else:
                telegram.send_text(self.s, f"❌ {key} 登录指令下发失败，请手动登录")
        except Exception as e:
            log.exception("远程登录触发失败")
            telegram.send_text(self.s, f"❌ 远程登录触发异常：{str(e)[:150]}")

    def _handle_url(self, raw_url: str,
                    target_platforms: list[str] | None = None,
                    unknown_tokens: list[str] | None = None) -> None:
        """处理单个 URL：规范化 → 查重 → 提取元数据 → 入库 → 回复。

        target_platforms: 指定发布平台（None = 全部启用平台）。
        unknown_tokens: 用户写了但没认出来的词，回复时提示。
        """
        clean_url = parse_url(raw_url)

        # 查重：source_url 是否已存在
        existing = self.db.find_by_source_url(clean_url)
        if existing:
            state = existing.get("state", "")
            label = _STATE_LABELS.get(state, state)
            shortcode = existing.get("shortcode", "?")
            if state in (State.PUBLISHED, State.NOTIFIED):
                telegram.send_text(
                    self.s,
                    f"✅ 该视频已发布过\n标识：{shortcode}\n状态：{label}\n链接：{clean_url}",
                )
            else:
                telegram.send_text(
                    self.s,
                    f"⏳ 该视频已在处理中\n标识：{shortcode}\n当前状态：{label}\n链接：{clean_url}",
                )
            return

        try:
            meta = extract_meta(clean_url)
        except ValueError as e:
            err = str(e)[:200]
            try:
                qid = self.db.enqueue_resolve(
                    raw_url, clean_url, target_platforms, err, delay_sec=120
                )
                pending = self.db.resolve_pending_count()
                if qid:
                    telegram.send_text(
                        self.s,
                        "\u23f3 \u89e3\u6790\u5931\u8d25\uff0c\u5df2\u52a0\u5165\u91cd\u8bd5\u961f\u5217\uff08#%d\uff09\n"
                        "\u539f\u56e0\uff1a%s\n"
                        "\u5f53\u524d\u961f\u5217\uff1a%d \u6761\u5f85\u91cd\u8bd5\n"
                        "\u94fe\u63a5\uff1a%s\n\n"
                        "\u00b7 5/10/20/40\u2026\u5206\u949f\u6307\u6570\u9000\u907f\uff0c\u6700\u591a 8 \u6b21\n"
                        "\u00b7 \u89e3\u6790\u6210\u529f\u540e\u81ea\u52a8\u8fdb\u6d41\u6c34\u7ebf\uff0c\u65e0\u9700\u91cd\u53d1"
                        % (qid, err, pending, clean_url),
                    )
                else:
                    telegram.send_text(
                        self.s,
                        "\u274c \u65e0\u6cd5\u89e3\u6790\u8be5\u94fe\u63a5\n%s\n\u94fe\u63a5\uff1a%s" % (err, clean_url),
                    )
            except Exception as qe:
                log.warning("\u5165\u91cd\u8bd5\u961f\u5217\u5931\u8d25\uff1a%s", qe)
                telegram.send_text(
                    self.s,
                    "\u274c \u65e0\u6cd5\u89e3\u6790\u8be5\u94fe\u63a5\n%s\n\u94fe\u63a5\uff1a%s" % (err, clean_url),
                )
            return

        # 再次查重（元数据可能有更精确的 source_url）
        existing2 = self.db.find_by_source_url(meta.source_url)
        if existing2:
            state = existing2.get("state", "")
            label = _STATE_LABELS.get(state, state)
            telegram.send_text(
                self.s,
                f"⏳ 该视频已存在（解析后匹配）\n标识：{existing2.get('shortcode','?')}\n状态：{label}",
            )
            return

        # 入库
        tid = self.db.insert_video(meta, target_platforms=target_platforms)
        if tid is None:
            telegram.send_text(self.s, f"⚠️ 该视频已入库（可能刚被处理）\n链接：{meta.source_url}")
            return

        platform_cn = _PLATFORM_CN.get(meta.platform, meta.platform)
        duration_str = f"{int(meta.duration)}秒" if meta.duration else "未知时长"
        if target_platforms:
            pub_names = "、".join(_PUBLISH_PLATFORM_CN.get(p, p) for p in target_platforms)
            pub_line = f"发布到：{pub_names}"
        else:
            # 默认发布平台（enabled 且 default）；头条等「按需投」平台不在其中
            from ..publish import default_publishers
            def_names = "、".join(_PUBLISH_PLATFORM_CN.get(p.name, p.name)
                                  for p in default_publishers(self.s))
            pub_line = f"发布到：{def_names or '（无默认平台）'}"
        warn_line = ""
        if unknown_tokens:
            warn_line = f"\n⚠️ 未识别的平台名：{', '.join(unknown_tokens)}"
        telegram.send_text(
            self.s,
            f"📥 已收到 [{platform_cn}] 视频，开始处理\n"
            f"来源：{meta.username or '未知'}\n"
            f"时长：{duration_str}\n"
            f"标识：{meta.shortcode}\n"
            f"{pub_line}{warn_line}\n\n"
            f"流水线：下载 → 转码 → 转录 → 文案 → 字幕 → 发布",
        )
        log.info("📥 收到新链接入库 [%s] tid=%d targets=%s", meta.shortcode, tid,
                 target_platforms or "all")
        if self._wakeup is not None:
            self._wakeup.set()


# 平台中文名（来源平台）
_PLATFORM_CN = {
    "instagram": "Instagram",
    "youtube": "YouTube",
    "facebook": "Facebook",
    "tiktok": "TikTok",
    "twitter": "Twitter",
}

# 发布平台别名 → canonical key（用户在消息里指定发布平台时用）
_PUBLISH_PLATFORM_ALIASES = {
    "快手": "kuaishou", "kuaishou": "kuaishou", "ks": "kuaishou",
    "快手2": "kuaishou2", "kuaishou2": "kuaishou2", "ks2": "kuaishou2",
    "抖音": "douyin", "douyin": "douyin", "dy": "douyin",
    "小红书": "xhs", "xhs": "xhs",
    "视频号": "weixin", "weixin": "weixin", "wx": "weixin", "微信视频号": "weixin",
    "今日头条": "toutiao", "头条": "toutiao", "头条号": "toutiao",
    "toutiao": "toutiao", "tt": "toutiao",
    # Instagram 矩阵号（出海线）
    # 国内用户习惯打中文，同时兼容 config 里的原名与去下划线写法
    "ig": "ig_pets", "ig萌宠": "ig_pets", "ig宠物": "ig_pets", "ig_pets": "ig_pets",
    "igpets": "ig_pets", "萌宠ig": "ig_pets",
    "ig解压": "ig_satisfying", "ig治愈": "ig_satisfying",
    "ig_satisfying": "ig_satisfying", "igsatisfying": "ig_satisfying", "解压ig": "ig_satisfying",
    "ig动画": "ig_anim", "ig_anim": "ig_anim", "iganim": "ig_anim", "动画ig": "ig_anim",
    "ig家居": "ig_home", "ig好物": "ig_home", "ig_home": "ig_home",
    "ighome": "ig_home", "家居ig": "ig_home", "ig带货": "ig_home",
}

# canonical key → 中文名（TG 回复用）
_PUBLISH_PLATFORM_CN = {
    "kuaishou": "快手",
    "kuaishou2": "快手2号",
    "douyin": "抖音",
    "xhs": "小红书",
    "weixin": "微信视频号",
    "toutiao": "今日头条",
    "ig_pets": "IG·萌宠",
    "ig_satisfying": "IG·解压",
    "ig_anim": "IG·动画",
    "ig_home": "IG·家居",
}


def _split_disabled_targets(s: Settings,
                            targets: list[str] | None) -> tuple[list[str] | None, list[str]]:
    """把指定平台拆成 (可用, 已停用)。

    停用 = config.yaml platforms.<name>.enabled 为 false。停用是硬性的：
    显式指定不能绕过（2026-10-10 起，视频号 IP 风控期间 @视频号 指定仍建
    job 发布，每条 15 分钟超时刷屏）。
    """
    if not targets:
        return None, []
    try:
        enabled = {sp.name for sp in enabled_publishers(s)}
    except Exception as e:
        log.warning("读取启用平台列表失败，按全部可用处理：%s", e)
        return targets, []
    kept = [t for t in targets if t in enabled]
    dropped = [t for t in targets if t not in enabled]
    return (kept or None), dropped


def _parse_target_platforms(text: str) -> tuple[list[str] | None, list[str]]:
    """从消息文本中解析发布平台指令。

    返回 (targets, unknown_tokens):
      - targets: 规范化后的平台 key 列表，None 表示未指定（全部平台）
      - unknown_tokens: 用户写了但没认出来的词（用于提示）
    """
    # 去掉 URL，只看剩余文本。sub 必须替换成空串 ——
    # 之前写成 sub(lambda m: m.group(0).rstrip(...)) 只是把 URL 原样写回，
    # 等于没删，URL 里的域名会被当成「未识别的平台名」提示给用户。
    remaining = _URL_RE.sub("", text)
    # 剥掉文件名里的扩展名：本地文件投链时平台标记常写在文件名里
    # （clip_@快手.mp4），不处理的话 token 是「快手.mp4」认不出来。
    remaining = re.sub(r"\.(?:mp4|mov|m4v|webm|mkv|avi|flv|wmv)\b",
                       "", remaining, flags=re.I)
    # 按空格、逗号、@、冒号分割成 token
    raw_tokens = re.split(r"[\s,@:：]+", remaining)
    # 这些词不是平台名，忽略
    _ignore = {"平台", "发到", "发", "到", "只", "发布"}
    targets: list[str] = []
    unknown: list[str] = []
    for tok in raw_tokens:
        tok = tok.strip()
        if not tok or tok in _ignore:
            continue
        key = _PUBLISH_PLATFORM_ALIASES.get(tok) or _PUBLISH_PLATFORM_ALIASES.get(tok.lower())
        if key:
            if key not in targets:
                targets.append(key)
        elif len(tok) <= 6 and not tok.startswith(("http", "/")):
            # 短词且非 URL/指令，当作可能是拼错的平台名
            unknown.append(tok)
    if not targets:
        return None, unknown
    return targets, unknown


def s_telegram_token(s: Settings) -> str:
    """取 token（避免在 f-string 里直接访问私有属性风格的命名）。"""
    return s.telegram_bot_token

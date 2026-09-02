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

from ..config import Settings
from ..db import Database, State
from ..notify import telegram
from .downloader import extract_meta, parse_url

log = logging.getLogger(__name__)

# URL 正则：匹配消息里的 http(s) 链接
_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)

# 审核/改文案/指定平台的文本指令前缀（TG callback_query 不方便时用文本走）
_EDIT_PREFIX = "edit:"
_TARGET_PREFIX = "target:"

# getUpdates long-poll 超时（秒）
_POLL_TIMEOUT = 30

# 状态中文映射（给用户看的回复）
_STATE_LABELS = {
    State.CANDIDATE: "待审核",
    State.PENDING_REVIEW: "待审核",
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
        telegram.notify_info(self.s, "🎧 投链监听已启动\n直接发视频链接给我即可（IG/YouTube/Facebook/TikTok 等）")
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                log.exception("TG 监听异常，30s 后重试：%s", e)
                time.sleep(30)

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

        for upd in data.get("result", []):
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

        text = msg.get("text") or ""
        if not text:
            return

        # 文本指令：改文案 / 指定平台（callback_query 不方便时的备选入口）
        t = text.strip()
        if t.lower().startswith(_EDIT_PREFIX):
            self._handle_edit_text(t)
            return
        if t.lower().startswith(_TARGET_PREFIX):
            self._handle_target_text(t)
            return

        # 提取 URL
        urls = _URL_RE.findall(text)
        if not urls:
            # 非 URL 消息：只响应明确的指令
            t_low = t.lower()
            if t_low in ("/help", "/start", "帮助"):
                telegram.send_text(
                    self.s,
                    "📬 直接发视频链接给我即可\n"
                    "支持：Instagram / YouTube / Facebook / TikTok 等\n"
                    "收到后会自动下载 → 转码 → 生成文案 → 加字幕 → 发布到快手/抖音/小红书\n"
                    "重复链接会自动判定，不会重复处理\n\n"
                    "💡 指定发布平台：链接后加平台名，如\n"
                    "  https://... @抖音 小红书\n"
                    "  https://... @douyin,xhs\n"
                    "不指定则发到所有启用的平台\n\n"
                    "🎬 发现层自动采集后发审核卡片，点按钮即可\n"
                    "  edit:<id> 你的标题    # 改文案\n"
                    "  target:<id> 抖音,小红书  # 指定发布平台",
                )
            return

        # 解析发布平台指令（链接之外的文本）
        target_platforms, unknown = _parse_target_platforms(text)

        for raw_url in urls:
            try:
                self._handle_url(raw_url, target_platforms, unknown)
            except Exception as e:
                log.exception("处理链接失败：%s", raw_url)
                telegram.send_text(self.s, f"❌ 处理链接失败：{str(e)[:200]}\n链接：{raw_url}")

    # ---------- callback_query 处理（审核按钮） ----------

    def _handle_callback(self, cq: dict) -> None:
        """处理审核卡片内联按钮回调。callback_data 格式：action:task_id。"""
        data = cq.get("data") or ""
        action, _, tid_str = data.partition(":")
        cq_id = cq.get("id", "")
        try:
            tid = int(tid_str)
        except ValueError:
            telegram.answer_callback(self.s, cq_id, "无效的回调数据")
            return

        task = self.db.get(tid)
        if not task:
            telegram.answer_callback(self.s, cq_id, "任务不存在")
            return

        # 只允许审核态任务被按钮操作
        if task.get("state") not in (State.CANDIDATE, State.PENDING_REVIEW):
            telegram.answer_callback(self.s, cq_id,
                                     f"该任务已 {task.get('state','')}，不可再审核")
            return

        if action == "approve":
            ok = self.db.approve(tid)
            if ok:
                telegram.answer_callback(self.s, cq_id,
                                         f"✅ 已通过，进入流水线：{task.get('shortcode','')}")
                # 更新原卡片文本（把按钮换成状态）
                msg = cq.get("message")
                if msg:
                    telegram.edit_message_text(
                        self.s, str(msg.get("chat", {}).get("id", "")),
                        msg.get("message_id", 0),
                        f"✅ 已通过 [{task.get('shortcode','')}]\n"
                        f"{task.get('title','')[:60]}\n"
                        f"状态：待下载处理"
                    )
                if self._wakeup is not None:
                    self._wakeup.set()
            else:
                telegram.answer_callback(self.s, cq_id, "操作失败（可能已被处理）")
        elif action == "reject":
            ok = self.db.reject(tid, "人工拒绝")
            if ok:
                telegram.answer_callback(self.s, cq_id, "❌ 已丢弃")
                msg = cq.get("message")
                if msg:
                    telegram.edit_message_text(
                        self.s, str(msg.get("chat", {}).get("id", "")),
                        msg.get("message_id", 0),
                        f"❌ 已丢弃 [{task.get('shortcode','')}]\n"
                        f"{task.get('title','')[:60]}"
                    )
            else:
                telegram.answer_callback(self.s, cq_id, "操作失败")
        elif action == "edit":
            telegram.answer_callback(
                self.s, cq_id,
                f"发新消息给我改好的标题，格式：edit:{tid} 你的标题文案",
                show_alert=True,
            )
        elif action == "platform":
            telegram.answer_callback(
                self.s, cq_id,
                f"发新消息：target:{tid} 抖音,小红书  （可选：快手/抖音/小红书/视频号）",
                show_alert=True,
            )
        else:
            telegram.answer_callback(self.s, cq_id, f"未知操作：{action}")

    # ---------- 文本指令处理（改文案 / 指定平台） ----------

    def _handle_edit_text(self, text: str) -> None:
        """edit:<id> <新标题>：更新任务的 title 后通过审核。"""
        body = text[len(_EDIT_PREFIX):].strip()
        tid_str, _, new_title = body.partition(" ")
        try:
            tid = int(tid_str)
        except ValueError:
            telegram.send_text(self.s, "格式：edit:<id> 你的新标题")
            return
        task = self.db.get(tid)
        if not task:
            telegram.send_text(self.s, f"任务 {tid} 不存在")
            return
        if task.get("state") not in (State.CANDIDATE, State.PENDING_REVIEW):
            telegram.send_text(self.s, f"任务 {tid} 已 {task.get('state')}，不可改文案")
            return
        new_title = new_title.strip()
        if not new_title:
            telegram.send_text(self.s, "标题不能为空")
            return
        self.db.update(tid, title=new_title)
        self.db.approve(tid)
        telegram.send_text(self.s, f"✏️ 已改文案并通过 [{task.get('shortcode','')}]\n"
                                    f"新标题：{new_title[:80]}")
        if self._wakeup is not None:
            self._wakeup.set()

    def _handle_target_text(self, text: str) -> None:
        """target:<id> 平台列表：指定发布平台后通过审核。"""
        body = text[len(_TARGET_PREFIX):].strip()
        tid_str, _, rest = body.partition(" ")
        try:
            tid = int(tid_str)
        except ValueError:
            telegram.send_text(self.s, "格式：target:<id> 抖音,小红书")
            return
        task = self.db.get(tid)
        if not task:
            telegram.send_text(self.s, f"任务 {tid} 不存在")
            return
        if task.get("state") not in (State.CANDIDATE, State.PENDING_REVIEW):
            telegram.send_text(self.s, f"任务 {tid} 已 {task.get('state')}，不可再指定平台")
            return
        # 解析平台名
        targets, unknown = _parse_target_platforms(rest)
        if not targets:
            telegram.send_text(self.s, "未识别平台名。可用：快手/抖音/小红书/视频号")
            return
        self.db.approve(tid, target_platforms=targets)
        pub_names = "、".join(_PUBLISH_PLATFORM_CN.get(p, p) for p in targets)
        telegram.send_text(self.s, f"🎯 已指定发布到 {pub_names}，通过审核 [{task.get('shortcode','')}]")
        if self._wakeup is not None:
            self._wakeup.set()

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

        # 提取元数据
        telegram.send_text(self.s, f"🔍 正在解析链接…\n{clean_url}")
        try:
            meta = extract_meta(clean_url)
        except ValueError as e:
            telegram.send_text(self.s, f"❌ 无法解析该链接\n{str(e)[:200]}\n链接：{clean_url}")
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
            pub_line = "发布到：所有启用平台"
        warn_line = ""
        if unknown_tokens:
            warn_line = f"\n⚠️ 未识别的平台名：{', '.join(unknown_tokens)}（可用：快手/抖音/小红书）"
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
    "抖音": "douyin", "douyin": "douyin", "dy": "douyin",
    "小红书": "xhs", "xhs": "xhs",
    "视频号": "weixin", "weixin": "weixin", "wx": "weixin", "微信视频号": "weixin",
}

# canonical key → 中文名（TG 回复用）
_PUBLISH_PLATFORM_CN = {
    "kuaishou": "快手",
    "douyin": "抖音",
    "xhs": "小红书",
    "weixin": "微信视频号",
}


def _parse_target_platforms(text: str) -> tuple[list[str] | None, list[str]]:
    """从消息文本中解析发布平台指令。

    返回 (targets, unknown_tokens):
      - targets: 规范化后的平台 key 列表，None 表示未指定（全部平台）
      - unknown_tokens: 用户写了但没认出来的词（用于提示）
    """
    # 去掉 URL，只看剩余文本
    remaining = _URL_RE.sub("", text)
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

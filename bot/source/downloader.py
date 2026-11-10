"""统一下载器：用 yt-dlp 从任意平台（IG/Facebook/YouTube/TikTok 等）下载视频。

替代旧版 instaloader 的 IG 直链下载，彻底摆脱 IG 小号风控。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import yt_dlp

from . import ig_api, ig_pool

log = logging.getLogger(__name__)

# cookies 文件（Netscape 格式）：存在则自动启用，解决 IG/YT 对数据中心 IP 的
# 强制登录风控（"empty media response" / "Sign in to confirm you're not a bot"）。
_COOKIE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "cookies.txt"
_COOKIE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cookies.d"


def _pick_cookies(url: str) -> tuple[Path | None, str]:
    """选 cookie 文件。

    - Instagram: 走多账号池（轮换 + 冷却）
    - 国内平台（抖音/小红书/快手/头条）: 优先选用专属 Cookie 文件 (data/cookies.d/*.txt)
    - 其它平台: 回退到遗留单文件 data/cookies.txt

    返回 (cookie 路径 or None, 账号名 or "")。
    """
    u = (url or "").lower()
    if "instagram.com" in u:
        cf = ig_pool.pick_cookiefile()
        if cf is not None:
            return cf, ig_pool.account_of(cf)

    # 国内主流短视频平台专用 Cookie 文件匹配（2026-11-10 引入）
    platform_cookie_map = {
        ("douyin.com", "iesdouyin.com"): "douyin.txt",
        ("xiaohongshu.com", "xhslink.com", "xhslink.cn"): "xhs.txt",
        ("kuaishou.com", "kwai.com"): "kuaishou.txt",
        ("toutiao.com",): "toutiao.txt",
        ("bilibili.com", "b23.tv"): "bilibili.txt",
    }
    for domains, filename in platform_cookie_map.items():
        if any(d in u for d in domains):
            p = _COOKIE_DIR / filename
            if p.exists() and p.stat().st_size > 0:
                return p, p.stem

    if _COOKIE_FILE.exists() and _COOKIE_FILE.stat().st_size > 0:
        return _COOKIE_FILE, ""
    return None, ""


def _impersonate_available() -> bool:
    """curl_cffi 是否可用（yt-dlp impersonate 的硬依赖）。

    2026-09-27 事故：Hetzner 迁移时 curl_cffi 没写进 requirements.txt，
    而本函数原来无条件设置 impersonate=chrome，导致**所有**链接处理都
    失败在 "Impersonate target chrome is not available"。现在先探测再设置：
    没装就跳过 impersonate（IG 等平台通常仍可下载，只是 YouTube 容易
    被风控），并在日志里明确提示，而不是让整条投链报错。
    """
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return False
    return True


def _base_opts(url: str = "") -> tuple[dict, str]:
    """所有 yt-dlp 会话共用的基础选项（含 cookies，若文件存在）。

    返回 (opts, ig_account)。ig_account 非空时调用方可用它回报成功/被封。
    """
    opts: dict = {}
    cf, acct = _pick_cookies(url)
    if cf is not None:
        opts["cookiefile"] = str(cf)
        log.info("yt-dlp 启用 cookies：%s%s", cf, f"（IG 账号 {acct}）" if acct else "")
    # YouTube 需要：浏览器指纹（curl_cffi）+ JS 运行时（deno）+ PO Token（bgutil 脚本）
    # + n-challenge 远程组件（等价 CLI 的 --remote-components ejs:github）
    if _impersonate_available():
        opts["impersonate"] = yt_dlp.networking.impersonate.ImpersonateTarget("chrome")
    else:
        log.warning("curl_cffi 未安装，已跳过浏览器指纹（impersonate）。"
                    "YouTube 可能触发风控，建议 pip install curl_cffi")
    opts["remote_components"] = ["ejs:github"]  # 顶层参数（YoutubeDL params），非 extractor_args
    # 超时保护下沉到基础选项，让 extract_meta 与 download 两条路径都生效。
    # yt-dlp 默认无 socket 超时：一条半死连接会让唯一的主循环线程永久挂住
    # （下载/转码/发布/DB 推进全部停摆），比 TG 监听被阻塞更严重。
    opts.setdefault("socket_timeout", 30)
    opts.setdefault("retries", 2)
    opts["noplaylist"] = True
    opts["extractor_args"] = {
        "youtube": {
            # android：实测能直接拿到 format 18（mp4 音视频合一），无需合并。
            # 2026-11-09 实测 7 个 client：
            #   mweb / web / ios / web_safari → Requested format is not available
            #   tv                           → The page needs to be reloaded
            #   android                      → ✅ format 18（mp4，含音频）
            #   tv_embedded                  → ✅ 但要 135+251 合并成 mkv，多一道转码
            # 优先 android 是因为单文件流最省事，也避免依赖 ffmpeg 的合并步骤。
            "player_client": ["android"],
        },
    }
    return opts, acct

# 追踪参数黑名单（出现在这些里的查询参数一律删掉）
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "igsh", "igshi", "igsi", "ref", "_branch")

# 平台识别：extractor_key → 短名称
_PLATFORM_MAP = {
    "Instagram": "instagram",
    "YouTube": "youtube",
    "Facebook": "facebook",
    "TikTok": "tiktok",
    "Twitter": "twitter",
    "YouTubeShorts": "youtube",
    # 国内素材源（出海线的上游）
    "Douyin": "douyin",
    "XiaoHongShu": "xhs",
    "BiliBili": "bilibili",
    "Weibo": "weibo",
}

# 国内平台的分享短链域名：yt-dlp 只能识别展开后的最终 URL。
# 抖音/小红书分享文案里给的是 v.douyin.com / xhslink.com 这类短链，
# 必须先把短链展开成 www.douyin.com/video/<id> 才能解析，
# 否则 yt-dlp 会直接报 Unsupported URL。
SHORT_LINK_HOSTS = {
    "v.douyin.com": "douyin",
    "xhslink.com": "xhs",
    "xhslink.cn": "xhs",
    "b23.tv": "bilibili",
}

# 从分享文案里抠出 URL（用户常整段转发：「7.89 复制打开抖音... https://v.douyin.com/xxx/」）
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\u4e00-\u9fff，。！？、；：\"'）】]+")


def extract_url_from_text(text: str) -> str:
    """从聊天文本里提取第一个 URL（支持国内 App 的整段分享文案）。

    国内分享文案形如：
        7.89 abc:/ 复制打开抖音，看看【xxx的作品】... https://v.douyin.com/iABC123/
    直接把这整段丢给 yt-dlp 必然失败，必须先正则抠出链接。
    """
    m = _URL_IN_TEXT_RE.search(text or "")
    return m.group(0) if m else ""


def expand_short_link(url: str, timeout: float = 10.0) -> tuple[str, str]:
    """展开国内平台短链，返回 (最终URL, 平台标识)。

    短视频平台为了 App 引流，分享出来的都是跳转短链（v.douyin.com/xxx），
    里面没有视频 ID，yt-dlp 无法解析。这里跟随一次 302 拿到真实地址。

    非短链（或展开失败）原样返回，平台标识为空 —— 让 yt-dlp 自己处理，
    失败也只是常规报错，不会因这一步把整条链路搞挂。
    """
    host = (urlparse(url).hostname or "").lower()
    platform = SHORT_LINK_HOSTS.get(host, "")
    if not platform:
        return url, ""

    try:
        import httpx
        # 必须不自动跟随，手动读 Location：短链服务有时会套多层跳转，
        # 且部分站点对 HEAD 请求返回 405，用 GET + 关掉 stream 更稳。
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": _BROWSER_UA}) as c:
            r = c.get(url)
            final = str(r.url)
        if final and final != url:
            log.info("短链已展开（%s）：%s -> %s", platform, url, final[:120])
            return final, platform
    except Exception as e:
        log.debug("短链展开失败（按原链接尝试）：%s", str(e)[:120])
    return url, platform


_BROWSER_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


@dataclass
class VideoMeta:
    """从 yt-dlp 提取的标准化视频元数据。"""
    source_url: str        # 规范化后的原始链接（去追踪参数）
    platform: str          # instagram / youtube / facebook / tiktok / unknown
    video_id: str          # 平台原生 ID（yt-dlp 的 id 字段），作为唯一去重键
    shortcode: str         # 统一标识符 {platform}_{video_id}，替代旧 IG shortcode
    username: str          # 上传者
    title: str             # 原始标题
    caption: str           # 原始描述
    thumbnail_url: str     # 缩略图 URL
    duration: float        # 时长（秒），0 表示未知
    permalink: str         # 视频原始链接


def parse_url(raw_url: str) -> str:
    """规范化链接：去追踪参数（igsi=、utm_*、fbclid= 等），返回干净 URL。"""
    parsed = urlparse(raw_url)
    if not parsed.scheme:
        parsed = parsed._replace(scheme="https")
    # 过滤查询参数
    if parsed.query:
        pairs = parse_qs(parsed.query, keep_blank_values=False)
        clean = {}
        for k, v in pairs.items():
            kl = k.lower()
            if any(kl.startswith(p) or kl == p for p in _TRACKING_PREFIXES):
                continue
            clean[k] = v[0] if len(v) == 1 else v
        new_query = urlencode(clean, doseq=True)
        parsed = parsed._replace(query=new_query)
    # 去 fragment
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)


def _platform_from_info(info: dict) -> str:
    key = info.get("extractor_key", "")
    return _PLATFORM_MAP.get(key, key.lower() or "unknown")


def extract_meta(url: str) -> VideoMeta:
    """用 yt-dlp 提取元数据（不下载）。失败抛 ValueError。

    会自动跳过非视频内容（图文/直播等）。
    """
    clean_url = parse_url(url)
    # 国内分享短链（v.douyin.com 等）先展开，否则 yt-dlp 直接 Unsupported URL
    clean_url, _ = expand_short_link(clean_url)

    # IG 优先走自研解析器；若自研失败（除纯图文帖外），回退到 yt-dlp 兜底
    if ig_api.is_instagram(clean_url):
        try:
            return ig_api.extract(clean_url)
        except ValueError as e:
            msg = str(e)
            if "纯图文" in msg:
                raise
            log.warning("自研 IG 解析失败，回退 yt-dlp 兜底：%s", msg[:150])

    # 抖音优先走自研免门禁解析器（绕过 ArgusSecurityPlugin 403 拦截并提取无水印原片）
    from . import douyin_api
    if douyin_api.is_douyin(clean_url):
        try:
            return douyin_api.extract(clean_url)
        except Exception as e:
            log.warning("自研抖音解析失败，回退 yt-dlp 兜底：%s", str(e)[:150])

    base, acct = _base_opts(clean_url)
    opts = {
        **base,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        # socket_timeout / retries 已在 _base_opts 统一设置（extract 与 download 共用）
        # 元数据阶段不选下载格式。mweb 在部分 Shorts 上没有可合并流，
        # 默认 bestvideo+bestaudio 会在拿到标题前就报 Requested format is not available。
        "format": None,
        "ignore_no_formats_error": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        # IG 风控：冷却该账号，下次自动换号
        if acct and ig_pool.is_blocked(msg):
            ig_pool.report_blocked(acct, msg)
            raise ValueError(
                f"IG 账号 {acct} 被风控，已冷却并切换其他账号。原始错误：{msg[:220]}"
            ) from e
        raise ValueError(f"yt-dlp 无法解析该链接：{msg[:300]}") from e

    if acct:
        ig_pool.report_ok(acct)

    if not info:
        raise ValueError("yt-dlp 返回空结果，可能链接无效或视频已删除")

    # playlist 或多视频类型跳过
    if info.get("_type") in ("playlist",):
        raise ValueError("该链接是播放列表，请发送单条视频链接")

    platform = _platform_from_info(info)
    video_id = str(info.get("id", ""))
    if not video_id:
        raise ValueError("无法提取视频 ID")

    shortcode = f"{platform}_{video_id}"
    uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
    title = info.get("title") or ""
    # description 可能在不同字段
    caption = info.get("description") or ""
    thumbnail = info.get("thumbnail") or ""
    duration = float(info.get("duration") or 0)

    return VideoMeta(
        source_url=clean_url,
        platform=platform,
        video_id=video_id,
        shortcode=shortcode,
        username=uploader,
        title=title,
        caption=caption,
        thumbnail_url=thumbnail,
        duration=duration,
        permalink=clean_url,
    )


def download(url: str, dest: Path) -> Path:
    """用 yt-dlp 下载视频到 dest（指定完整文件路径）。

    格式选择：优先 mp4，fallback 到 best。
    失败抛 yt_dlp.utils.DownloadError。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    clean_url = parse_url(url)
    # 短链展开（与 extract_meta 保持一致：两处都必须展开，
    # 否则会出现「元数据拿到了、下载却报 Unsupported URL」的割裂）
    clean_url, _ = expand_short_link(clean_url)

    # 抖音优先走自研无水印直链极速下载（秒级流式落盘，绕过 yt-dlp 403）
    from . import douyin_api
    if douyin_api.is_douyin(clean_url):
        try:
            return douyin_api.download(clean_url, dest)
        except Exception as e:
            log.warning("自研抖音下载失败，回退 yt-dlp 兜底：%s", str(e)[:150])

    # yt-dlp 的 outtmpl 控制：我们指定完整文件名，用 dest 的 stem + dir
    out_dir = str(dest.parent)
    out_name = dest.stem  # 不含扩展名，yt-dlp 会自动加

    opts = {
        **_base_opts(url)[0],
        "quiet": True,
        "no_warnings": True,
        "outtmpl": f"{out_dir}/{out_name}.%(ext)s",
        # 格式选择（2026-11-09 修正）
        # ----------------------------
        # 旧值 "best[ext=mp4]/bestvideo*+bestaudio/best" 会失败：
        # YouTube 现在把音视频分开给（m4a / mp4 只有画面），没有
        # 「带音频的 mp4 单流」。而 best[ext=mp4] 会匹配到 233/234 这类
        # storyboard 占位格式（vcodec=none），yt-dlp 判定不可用后**直接抛错**，
        # 不再回退到后面的备用选择器。
        # 实测该视频：best[ext=mp4] 失败、bestvideo*+bestaudio 成功（135+251）。
        # 所以把「分离流合并」放在第一位，它才是 YouTube 的常规形态。
        "format": "bestvideo*+bestaudio/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        # 下载后如果格式不是 mp4，自动转码
        "postprocessors": [],
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([clean_url])

    # yt-dlp 可能输出 .mp4 或其他扩展名，找实际文件
    if dest.exists():
        return dest
    # 尝试常见扩展名
    for ext in ("mp4", "webm", "mkv", "m4v"):
        candidate = dest.parent / f"{out_name}.{ext}"
        if candidate.exists():
            if ext != "mp4":
                # 非 mp4 则重命名为目标（后续 ffmpeg 会转码）
                candidate.rename(dest)
            return dest
    raise FileNotFoundError(f"yt-dlp 下载完成但找不到输出文件：{dest}")

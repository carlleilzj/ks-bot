"""统一下载器：用 yt-dlp 从任意平台（IG/Facebook/YouTube/TikTok 等）下载视频。

替代旧版 instaloader 的 IG 直链下载，彻底摆脱 IG 小号风控。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import yt_dlp

from . import ig_api, ig_pool

log = logging.getLogger(__name__)

# cookies 文件（Netscape 格式）：存在则自动启用，解决 IG/YT 对数据中心 IP 的
# 强制登录风控（"empty media response" / "Sign in to confirm you're not a bot"）。
_COOKIE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "cookies.txt"


def _pick_cookies(url: str) -> tuple[Path | None, str]:
    """选 cookie 文件。IG 走多账号池，其余平台用遗留单文件。

    返回 (cookie 路径 or None, 账号名 or "")。
    """
    if "instagram.com" in (url or "").lower():
        cf = ig_pool.pick_cookiefile()
        if cf is not None:
            return cf, ig_pool.account_of(cf)
    if _COOKIE_FILE.exists() and _COOKIE_FILE.stat().st_size > 0:
        return _COOKIE_FILE, ""
    return None, ""


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
    opts["impersonate"] = yt_dlp.networking.impersonate.ImpersonateTarget("chrome")
    opts["remote_components"] = ["ejs:github"]  # 顶层参数（YoutubeDL params），非 extractor_args
    opts["extractor_args"] = {
        "youtube": {
            # mweb：实测可绕过 SABR 流限制拿到完整音视频 URL（web_safari 会被 SABR 全灭）
            "player_client": ["mweb"],
        },
    }
    return opts, acct

# 追踪参数黑名单（出现在这些里的查询参数一律删掉）
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "igshi", "igsi", "ref", "_branch")

# 平台识别：extractor_key → 短名称
_PLATFORM_MAP = {
    "Instagram": "instagram",
    "YouTube": "youtube",
    "Facebook": "facebook",
    "TikTok": "tiktok",
    "Twitter": "twitter",
    "YouTubeShorts": "youtube",
}


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

    # IG 优先走自研解析器；若自研失败（除纯图文帖外），回退到 yt-dlp 兜底
    if ig_api.is_instagram(clean_url):
        try:
            return ig_api.extract(clean_url)
        except ValueError as e:
            msg = str(e)
            if "纯图文" in msg:
                raise
            log.warning("自研 IG 解析失败，回退 yt-dlp 兜底：%s", msg[:150])

    base, acct = _base_opts(clean_url)
    opts = {
        **base,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
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

    # yt-dlp 的 outtmpl 控制：我们指定完整文件名，用 dest 的 stem + dir
    out_dir = str(dest.parent)
    out_name = dest.stem  # 不含扩展名，yt-dlp 会自动加

    opts = {
        **_base_opts(url)[0],
        "quiet": True,
        "no_warnings": True,
        "outtmpl": f"{out_dir}/{out_name}.%(ext)s",
        "format": "best[ext=mp4]/bestvideo*+bestaudio/best",
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

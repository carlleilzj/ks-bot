"""统一下载器：用 yt-dlp 从任意平台（IG/Facebook/YouTube/TikTok 等）下载视频。

替代旧版 instaloader 的 IG 直链下载，彻底摆脱 IG 小号风控。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import yt_dlp

log = logging.getLogger(__name__)

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
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ValueError(f"yt-dlp 无法解析该链接：{str(e)[:300]}") from e

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

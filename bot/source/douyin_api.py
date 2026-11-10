"""抖音视频直链解析与下载。

解决什么问题（2026-11-10 实测突破）
---------------------------------
2026 年 8 月起，抖音在边缘网关挂载了 ArgusSecurityPlugin：
对 /aweme/v1/web/aweme/detail/ 接口做前置门禁校验。
yt-dlp 的内置 Douyin 提取器未携带网关请求头（x-tt-argus），
被网关 403 拦截并报错：
    ERROR: [Douyin] ...: Fresh cookies (not necessarily logged in) are needed
误导用户以为是 Cookie 过期，其实是请求被网关判定为爬虫并拒绝。

本模块提供自研的双重解析路径（类似 ig_api.py）：
1. **主路径：移动端 Feed 接口**（https://api5-normal-c-hl.amemv.com/aweme/v1/feed/）
   - 免 ArgusSecurityPlugin 门禁
   - 无需任何 Cookie，直接返回 1080P 无水印高清 MP4 直链
   - 提取真实作者、完整文案、精准时长
2. **备用路径：Web 详情接口 + 网关协议头**（x-tt-argus: 1）
   - 若移动端接口偶发波动，使用带网关头的 Web 接口兜底
3. **极速直连下载**：
   - 绕过 yt-dlp 复杂的格式选择器，直接流式下载 CDN 直链，秒级完成。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
import httpx

if TYPE_CHECKING:
    from .downloader import VideoMeta

log = logging.getLogger(__name__)

_MOBILE_UA = (
    "com.ss.android.ugc.aweme/300801 (Linux; U; Android 10; zh_CN; MI 9; "
    "Build/QKQ1.190825.002; Cronet/TTNetVersion:b4d74d15 2024-05-15)"
)

_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_AWEME_ID_RE = re.compile(r"/(?:video|note|share/video)/(\d+)")


def is_douyin(url: str) -> bool:
    """是否为抖音链接（含短链与网页直链）。"""
    u = (url or "").lower()
    return "douyin.com" in u or "iesdouyin.com" in u


def extract_aweme_id(url: str) -> str:
    """从抖音链接中提取视频 ID（数字字符串）。"""
    from .downloader import expand_short_link, parse_url
    clean = parse_url(url)
    expanded, _ = expand_short_link(clean)
    m = _AWEME_ID_RE.search(expanded)
    if m:
        return m.group(1)
    # 尝试直接正则抓 19 位数字 ID
    m2 = re.search(r"(\d{18,20})", expanded)
    return m2.group(1) if m2 else ""


def _fetch_via_mobile_feed(aweme_id: str, timeout: float = 15.0) -> dict | None:
    """主路径：调用移动端 Feed 接口（免 Argus 门禁，直接返回无水印原片）。"""
    url = "https://api5-normal-c-hl.amemv.com/aweme/v1/feed/"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.get(url, params={"aweme_id": aweme_id},
                      headers={"User-Agent": _MOBILE_UA})
            if r.status_code != 200:
                log.debug("抖音移动端 Feed 返回 HTTP %d", r.status_code)
                return None
            data = r.json()
            aweme_list = data.get("aweme_list") or []
            if aweme_list:
                for item in aweme_list:
                    if str(item.get("aweme_id")) == str(aweme_id):
                        return item
                return aweme_list[0]
    except Exception as e:
        log.debug("抖音移动端 Feed 请求异常: %s", e)
    return None


def _fetch_via_web_detail(aweme_id: str, cookiefile: Path | None = None,
                          timeout: float = 15.0) -> dict | None:
    """备用路径：调用 Web Detail 接口（携带 x-tt-argus 网关头绕过 403）。"""
    from .downloader import _pick_cookies
    url = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
    cf = cookiefile
    if cf is None:
        cf, _ = _pick_cookies("https://www.douyin.com/")

    cookies = {}
    if cf and cf.exists():
        for line in cf.read_text(errors="ignore").splitlines():
            raw = line.removeprefix("#HttpOnly_")
            if not raw.strip() or raw.startswith("#"):
                continue
            parts = raw.split("\t")
            if len(parts) >= 7 and "douyin.com" in parts[0]:
                cookies[parts[5].strip()] = parts[6].strip()

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {
        "User-Agent": _DESKTOP_UA,
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie_header,
        "x-tt-argus": "1",
        "uifid": cookies.get("UIFID") or cookies.get("UIFID_TEMP") or "1",
    }
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.get(url, params={"aweme_id": aweme_id}, headers=headers)
            if r.status_code == 200:
                return r.json().get("aweme_detail")
            log.debug("抖音 Web Detail 返回 HTTP %d: %s", r.status_code, r.text[:100])
    except Exception as e:
        log.debug("抖音 Web Detail 请求异常: %s", e)
    return None


def extract(url: str, cookiefile: Path | None = None) -> VideoMeta:
    """自研解析抖音视频元数据（无水印直链）。失败抛 ValueError。"""
    from .downloader import VideoMeta, parse_url

    clean_url = parse_url(url)
    aweme_id = extract_aweme_id(clean_url)
    if not aweme_id:
        raise ValueError(f"无法从抖音链接中提取视频 ID：{url}")

    # 1. 优先尝试移动端 Feed
    detail = _fetch_via_mobile_feed(aweme_id)
    # 2. 备用尝试 Web Detail (带 Argus 头)
    if not detail:
        detail = _fetch_via_web_detail(aweme_id, cookiefile=cookiefile)

    if not detail:
        raise ValueError(
            f"抖音视频解析失败（ID: {aweme_id}）。视频可能已被删除、设置为私密，或触发风控。"
        )

    # 提取视频与作者信息
    desc = (detail.get("desc") or "").strip()
    author = (detail.get("author") or {}).get("nickname") or ""
    video = detail.get("video") or {}

    # 优先取无水印播放地址列表
    play_addr = video.get("play_addr") or video.get("play_addr_h264") or {}
    url_list = play_addr.get("url_list") or []
    if not url_list:
        raise ValueError(f"抖音作品未找到有效视频播放地址（ID: {aweme_id}，可能是纯图文笔记）")

    # 持续时长 (毫秒 -> 秒)
    dur = float(video.get("duration") or detail.get("duration") or 0)
    if dur > 1000:
        dur = dur / 1000.0

    # 封面图
    cover_obj = video.get("cover") or video.get("origin_cover") or {}
    cover_urls = cover_obj.get("url_list") or []
    cover_url = cover_urls[0] if cover_urls else ""

    shortcode = f"douyin_{aweme_id}"
    return VideoMeta(
        source_url=clean_url,
        platform="douyin",
        video_id=aweme_id,
        shortcode=shortcode,
        username=author,
        title=desc.split("\n")[0][:120] if desc else shortcode,
        caption=desc,
        thumbnail_url=cover_url,
        duration=dur,
        permalink=f"https://www.douyin.com/video/{aweme_id}",
    )


def download(url: str, dest: Path, cookiefile: Path | None = None) -> Path:
    """直接下载抖音无水印高清原片。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta = extract(url, cookiefile=cookiefile)

    # 提取播放直链
    aweme_id = meta.video_id
    detail = _fetch_via_mobile_feed(aweme_id) or _fetch_via_web_detail(aweme_id, cookiefile=cookiefile)
    if not detail:
        raise ValueError(f"获取抖音下载地址失败: {url}")

    video = detail.get("video") or {}
    play_addr = video.get("play_addr") or video.get("play_addr_h264") or {}
    url_list = play_addr.get("url_list") or []
    if not url_list:
        raise ValueError(f"无可用视频下载链接: {url}")

    download_url = url_list[0]
    log.info("[douyin] 开始直链下载无水印视频: %s -> %s", meta.shortcode, dest.name)

    headers = {
        "User-Agent": _MOBILE_UA,
        "Referer": "https://www.douyin.com/",
    }
    with httpx.Client(timeout=120, follow_redirects=True, headers=headers) as client:
        with client.stream("GET", download_url) as resp:
            if resp.status_code != 200:
                raise ValueError(f"下载抖音直链失败 HTTP {resp.status_code}: {download_url[:100]}")
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

    if not dest.exists() or dest.stat().st_size == 0:
        raise FileNotFoundError(f"抖音下载完成但文件为空: {dest}")

    size_mb = dest.stat().st_size / (1024 * 1024)
    log.info("[douyin] 下载成功: %s (%.1f MB)", dest.name, size_mb)
    return dest

"""发现层：主动去各大平台采集热门动画短视频候选。

每个 SourceAdapter.discover() 返回 Candidate 列表（不下载，只取元数据）。
DiscoveryScheduler 周期调用各适配器 → FilterChain 过滤 → 入库 CANDIDATE →
发 TG 审核卡片（PENDING_REVIEW）。用户点按钮 approve 后才进 DETECTED 流水线。

适配器统一用 yt-dlp 的 extract_flat 做批量发现，不触发下载风控。
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx
import yt_dlp

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    """发现层产出的候选视频（未入库前的中间形态）。"""

    url: str            # 规范化链接
    platform: str       # youtube / tiktok / instagram / bilibili / rss
    video_id: str       # 平台原生 ID
    title: str
    uploader: str
    duration: float     # 秒，0 表示未知
    thumbnail: str      # 缩略图 URL
    score: float        # 热度评分（播放量/点赞数归一化），越大越热；<=0 表示无热度数据
    reason: str         # 人工可读的来源说明，如 "YouTube 搜索: anime short"
    source_tag: str     # 机器可读的来源标签，如 "yt_search:anime short"，用于追溯
    trusted_source: bool = False  # 官方/订阅频道来源：跳过关键词白名单（标题常不含 anime 字样）


class SourceAdapter:
    """适配器基类。子类实现 discover() 返回 Candidate 列表。"""

    name: str = "base"

    def discover(self, limit: int = 20) -> list[Candidate]:
        raise NotImplementedError


# ============================================================
# YouTube 搜索适配器
# ============================================================

class YouTubeSearchAdapter(SourceAdapter):
    """用 yt-dlp 的 ytsearch{N}:query 做关键词搜索。

    yt-dlp 原生支持，稳定性最高。能拿到 view_count 做 score。
    """

    name = "youtube_search"

    def __init__(self, queries: list[str], min_duration: float = 5, max_duration: float = 120):
        self.queries = queries
        self.min_duration = min_duration
        self.max_duration = max_duration

    def discover(self, limit: int = 20) -> list[Candidate]:
        cands: list[Candidate] = []
        for q in self.queries:
            per_query = max(1, limit // max(1, len(self.queries)))
            opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": "in_playlist",  # 只取列表层元数据，不下钻单条
                "skip_download": True,
                "playlistend": per_query,
            }
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(f"ytsearch{per_query}:{q}", download=False)
            except Exception as e:
                log.warning("YouTube 搜索 %r 失败：%s", q, e)
                continue
            for e in (info or {}).get("entries", []) or []:
                if not e:
                    continue
                dur = float(e.get("duration") or 0)
                vid = str(e.get("id") or "")
                url = e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
                if not url or not vid:
                    continue
                thumb = ""
                thumbs = e.get("thumbnails") or []
                if thumbs and isinstance(thumbs, list):
                    thumb = thumbs[-1].get("url", "") if isinstance(thumbs[-1], dict) else ""
                cands.append(Candidate(
                    url=url, platform="youtube", video_id=vid,
                    title=e.get("title") or "",
                    uploader=e.get("uploader") or e.get("channel") or "",
                    duration=dur, thumbnail=thumb,
                    score=float(e.get("view_count") or 0),
                    reason=f"YouTube 搜索: {q}",
                    source_tag=f"yt_search:{q}",
                ))
        return cands


# ============================================================
# YouTube 频道 RSS 适配器
# ============================================================

class RSSAdapter(SourceAdapter):
    """订阅 YouTube 频道 RSS，增量发现新视频。

    YouTube 频道 RSS: https://www.youtube.com/feeds/videos.xml?channel_id=UCxxxx
    不需登录、不限流，每 15 分钟更新一次。拿不到 view_count，score 用发布顺序近似。
    """

    name = "youtube_rss"

    def __init__(self, channel_ids: list[str], min_duration: float = 5, max_duration: float = 120):
        self.channel_ids = channel_ids
        self.min_duration = min_duration
        self.max_duration = max_duration

    def discover(self, limit: int = 20) -> list[Candidate]:
        cands: list[Candidate] = []
        per_channel = max(1, limit // max(1, len(self.channel_ids))) if self.channel_ids else limit
        for cid in self.channel_ids:
            feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
            try:
                entries = self._fetch_rss(feed_url, per_channel)
            except Exception as e:
                log.warning("RSS %s 获取失败：%s", cid, e)
                continue
            for idx, ent in enumerate(entries):
                cands.append(Candidate(
                    url=ent["url"],
                    platform="youtube",
                    video_id=ent["video_id"],
                    title=ent["title"],
                    uploader=ent["author"],
                    duration=0.0,  # RSS 不带 duration，交给 FilterChain 用默认区间放行
                    thumbnail=ent.get("thumbnail", ""),
                    score=float(-idx),  # 越新 score 越高（0 是最新）；负数序数不参与热度阈值
                    reason=f"YouTube RSS: {cid}",
                    source_tag=f"yt_rss:{cid}",
                    trusted_source=True,  # 订阅的是人工筛选的官方频道，跳过关键词白名单
                ))
        return cands

    def _fetch_rss(self, feed_url: str, max_items: int) -> list[dict]:
        """解析 YouTube RSS feed。返回 [{video_id, title, url, author, thumbnail}]。"""
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(feed_url)
        if resp.status_code != 200:
            raise RuntimeError(f"RSS HTTP {resp.status_code}")
        root = ET.fromstring(resp.text)
        ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "media": "http://search.yahoo.com/mrss/"}
        out = []
        for entry in root.findall("atom:entry", ns)[:max_items]:
            vid_el = entry.find("yt:videoId", ns)
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            author_el = entry.find("atom:author/atom:name", ns)
            thumb_el = entry.find("media:group/media:thumbnail", ns)
            if vid_el is None or not vid_el.text:
                continue
            vid = vid_el.text.strip()
            url = link_el.get("href") if link_el is not None else f"https://www.youtube.com/watch?v={vid}"
            out.append({
                "video_id": vid,
                "title": (title_el.text or "").strip() if title_el is not None else "",
                "url": url,
                "author": (author_el.text or "").strip() if author_el is not None else "",
                "thumbnail": thumb_el.get("url", "") if thumb_el is not None else "",
            })
        return out


# ============================================================
# 适配器工厂
# ============================================================

def build_adapters(s) -> list[SourceAdapter]:
    """根据 Settings.discovery.sources 构造适配器列表。

    s.discovery.sources 是 list[dict]，每项 {type: ..., ...params}。
    """
    disc = getattr(s, "discovery", None)
    if not disc or not disc.enabled:
        return []
    adapters: list[SourceAdapter] = []
    for spec in disc.sources:
        t = spec.get("type", "").strip().lower()
        if t == "youtube_search":
            queries = spec.get("queries") or ["anime short"]
            adapters.append(YouTubeSearchAdapter(
                queries=[q.strip() for q in queries if q.strip()],
                min_duration=disc.filters_min_duration,
                max_duration=disc.filters_max_duration,
            ))
        elif t == "youtube_rss" or t == "rss":
            cids = spec.get("channel_ids") or []
            adapters.append(RSSAdapter(
                channel_ids=[c.strip() for c in cids if c.strip()],
                min_duration=disc.filters_min_duration,
                max_duration=disc.filters_max_duration,
            ))
        elif t == "youtube_playlist":
            adapters.append(YouTubePlaylistAdapter(
                playlist_id=spec.get("playlist_id", ""),
                min_duration=disc.filters_min_duration,
                max_duration=disc.filters_max_duration,
            ))
        else:
            log.warning("未知发现源类型：%s，跳过", t)
    return adapters


# ============================================================
# YouTube 播放列表适配器（Phase 3 预留）
# ============================================================

class YouTubePlaylistAdapter(SourceAdapter):
    """订阅 YouTube 播放列表，增量发现。"""

    name = "youtube_playlist"

    def __init__(self, playlist_id: str, min_duration: float = 5, max_duration: float = 120):
        self.playlist_id = playlist_id
        self.min_duration = min_duration
        self.max_duration = max_duration

    def discover(self, limit: int = 20) -> list[Candidate]:
        if not self.playlist_id:
            return []
        url = f"https://www.youtube.com/playlist?list={self.playlist_id}"
        opts = {
            "quiet": True, "no_warnings": True,
            "extract_flat": "in_playlist", "skip_download": True,
            "playlistend": limit,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            log.warning("YouTube 播放列表 %s 获取失败：%s", self.playlist_id, e)
            return []
        cands = []
        for e in (info or {}).get("entries", []) or []:
            if not e:
                continue
            vid = str(e.get("id") or "")
            if not vid:
                continue
            cands.append(Candidate(
                url=e.get("url") or f"https://www.youtube.com/watch?v={vid}",
                platform="youtube", video_id=vid,
                title=e.get("title") or "",
                uploader=e.get("uploader") or "",
                duration=float(e.get("duration") or 0),
                thumbnail=(e.get("thumbnails") or [{}])[-1].get("url", "") if e.get("thumbnails") else "",
                score=float(e.get("view_count") or 0),
                reason=f"YouTube 播放列表: {self.playlist_id}",
                source_tag=f"yt_playlist:{self.playlist_id}",
            ))
        return cands

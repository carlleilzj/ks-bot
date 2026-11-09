"""自动素材发现：从多个来源按赛道找新视频，替代人工投链。

为什么需要
----------
到 2026-11-09 为止，整条链路唯一需要人工的地方是「找到素材链接」：
TG 投链 → 下载 → 转码 → 生成文案 → 发布 全部自动，但发现靠人。

人工发现的代价在 B 号上暴露得很清楚：10-24~11-02 床品占 71%，
11-03 之后掉到 45%，美妆教程/电竞房/女鞋混进来 27%。人靠眼睛挑，
挑着挑着就漂了。

设计原则
--------
1. **赛道隔离**：每个平台有自己的 lane，素材不串号。
   B 号是家居号，绝不能收到动物动画；A 号是搞笑号，也不该收床品。
2. **关键词必须命中**（require_any）：光靠 hashtag/搜索词不够，
   平台会把「床垫广告里的模特化妆」也归到 bedding。命中的是 caption，
   不是标签。
3. **来源可插拔**：IG 风控严、YouTube 宽松、后续可能加 TikTok。
   每个来源实现 discover(query, limit) -> list[Candidate]，失败只影响自己。
4. **只发现不下载**：候选进入既有流水线（去重/下载/转码），
   复用已经跑通的路径，不另造一套。

数据源现状（2026-11-09 实测）
---------------------------
- IG：所有发现接口都要 sessionid（web_profile_info 429、topsearch 404），
  当前 cookie 没有 sessionid，只有匿名 cookie。yt-dlp 的 IG user 提取器
  已被官方标记 broken。**IG 发现暂不可用**，但单条 Reel 解析是好的。
- YouTube：ytsearch 与 channel 列表都正常，且搜索结果质量高得多
  （实测 ytsearch "bedroom bedding cozy home decor short" 返回的全是
  真实家居内容）。当前主要来源。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    """一条待评估的素材。"""
    url: str
    title: str = ""
    description: str = ""
    source: str = ""            # youtube / instagram / manual
    channel: str = ""
    duration: float = 0.0
    # 命中的赛道关键词（空 = 未通过题材过滤）
    matched_keywords: list[str] = field(default_factory=list)

    @property
    def hint_text(self) -> str:
        """用于关键词匹配的文本：标题 + 描述。"""
        return f"{self.title} {self.description}"


@dataclass
class LaneConfig:
    """一条赛道（对应一个发布平台）。"""
    name: str
    # 搜索词：每个词会作为一次独立查询
    queries: list[str] = field(default_factory=list)
    # 必须命中其中之一（caption 级），否则丢弃。
    # 空 = 不做题材过滤（如 A 号不限制）。
    require_any: list[str] = field(default_factory=list)
    # 命中即丢弃（比 require_any 优先级更高）
    block_any: list[str] = field(default_factory=list)
    # 只看这个时长区间（秒）。短视频平台，超过 180 秒基本不适合。
    min_duration: float = 5.0
    max_duration: float = 180.0
    # 每次运行最多收多少条
    per_run: int = 8


def _norm_text(s: str) -> str:
    return (s or "").lower()


def match_keywords(text: str, keywords: list[str]) -> list[str]:
    """返回 text 里命中的关键词（大小写不敏感，支持中英文）。"""
    low = _norm_text(text)
    return [k for k in keywords if k and _norm_text(k) in low]


def evaluate(cand: Candidate, lane: LaneConfig) -> tuple[bool, str]:
    """判断候选是否可进入该赛道。返回 (是否通过, 原因)。"""
    if cand.duration and not (lane.min_duration <= cand.duration <= lane.max_duration):
        return False, f"时长 {cand.duration:.0f}s 不在 {lane.min_duration:.0f}~{lane.max_duration:.0f}s"

    blocked = match_keywords(cand.hint_text, lane.block_any)
    if blocked:
        return False, f"命中排除词：{','.join(blocked)}"

    if lane.require_any:
        hit = match_keywords(cand.hint_text, lane.require_any)
        if not hit:
            return False, "未命中赛道关键词"
        cand.matched_keywords = hit

    return True, "通过"


# ---------------------------------------------------------------------------
# 来源：YouTube
# ---------------------------------------------------------------------------

def discover_youtube(query: str, limit: int = 8,
                     search_prefix: str = "ytsearch") -> list[Candidate]:
    """用 yt-dlp 的 ytsearch 找候选。

    为什么用 ytsearch 而不是频道订阅：
    频道更新慢（家居号一天 1~2 条），搜索能持续供给新素材，而且
    yt-dlp 对 YouTube 的支持比 IG 稳得多（实测 2026-11-09：IG 的
    user 提取器已被官方标记 broken，YouTube 完全正常）。
    """
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": max(1, limit),
        "skip_download": True,
        "socket_timeout": 20,
    }
    url = f"{search_prefix}{limit}:{query}"
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
    except Exception as e:
        log.warning("YouTube 搜索失败（%s）：%s", query, str(e)[:160])
        return []

    out: list[Candidate] = []
    for e in (info.get("entries") or []):
        vid = e.get("id") or ""
        if not vid:
            continue
        # extract_flat 时字段有限：duration 可能缺失，交给下游实际下载时判断
        out.append(Candidate(
            url=f"https://www.youtube.com/watch?v={vid}",
            title=e.get("title") or "",
            description=e.get("description") or "",
            source="youtube",
            channel=e.get("channel") or e.get("uploader") or "",
            duration=float(e.get("duration") or 0),
        ))
    return out


# ---------------------------------------------------------------------------
# 来源：Instagram（当前不可用，保留接口）
# ---------------------------------------------------------------------------

def discover_instagram(query: str, limit: int = 8) -> list[Candidate]:
    """IG hashtag/账号发现。当前不可用，返回空并说明原因。

    阻塞点（2026-11-09 实测）：
    - /api/v1/users/web_profile_info/  → HTTP 429（无 sessionid 视为匿名）
    - /api/v1/web/search/topsearch/    → HTTP 404 返回 HTML 登录页
    - yt-dlp 的 instagram:user 提取器被官方标记 broken
    只有单条 Reel 的解析（bot/source/ig_api.py）是正常的。

    要恢复需重新导出一个**含 sessionid** 的 cookie 文件。
    """
    log.debug("IG 发现跳过（缺 sessionid，见模块 docstring）")
    return []


SOURCES = {
    "youtube": discover_youtube,
    "instagram": discover_instagram,
}


def lane_from_config(name: str, conf: dict) -> LaneConfig:
    """把 config.yaml 的 platforms.<name>.discover 段转成 LaneConfig。

    宽容处理：字段缺失/类型不对时用默认值，绝不因为配置写错导致整条
    发现链路崩掉 —— 发现是增量能力，坏了应该退化成「没有新素材」，
    而不是让发布主流程跟着挂。
    """
    def _strs(v) -> list[str]:
        if not isinstance(v, (list, tuple)):
            return []
        return [str(x).strip() for x in v if str(x).strip()]

    def _num(v, default):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    return LaneConfig(
        name=name,
        queries=_strs(conf.get("queries")),
        require_any=_strs(conf.get("require_any")),
        block_any=_strs(conf.get("block_any")),
        min_duration=_num(conf.get("min_duration"), 5.0),
        max_duration=_num(conf.get("max_duration"), 180.0),
        per_run=int(_num(conf.get("per_run"), 8)),
    )


def sources_from_config(conf: dict) -> list[str]:
    """配置里的 sources 列表，过滤掉不认识的来源。"""
    raw = conf.get("sources")
    if not isinstance(raw, (list, tuple)):
        return ["youtube"]
    picked = [str(x).strip() for x in raw if str(x).strip()]
    return [s for s in picked if s in SOURCES] or ["youtube"]


# ---------------------------------------------------------------------------
# 调度器：发现 → 元数据 → 入库
# ---------------------------------------------------------------------------

# 每个 lane 上次运行时间（内存态；进程重启后重新计时，不会重复跑）
_last_run: dict[str, float] = {}


def run_discovery(s, db, force: bool = False) -> dict:
    """按配置为各平台跑一轮自动发现并把候选入库。

    返回 {"platform": {"found": n, "new": n, "skipped": n, "errors": [...]}}

    设计取舍
    --------
    * 只入库，不下载。下载/转码/文案/发布全走既有流水线，
      避免发现层出 bug 时连带把成品也搞坏。
    * 每条候选都要重新 extract_meta（拿真实时长、准确 caption），
      因为 ytsearch 的 extract_flat 结果字段不全，光靠它过滤会漏。
    * 单条失败不中断：一个候选解析失败只记日志，不影响同批其他候选。
    """
    from .downloader import extract_meta

    if not force and not _discovery_due(s):
        return {}

    summary: dict[str, dict] = {}
    for name, plat in s.platforms.items():
        conf = (plat.discover or {})
        if not conf.get("enabled"):
            continue
        lane = lane_from_config(name, conf)
        if not lane.queries:
            log.warning("[%s] 自动发现已启用但没有 queries，跳过", name)
            continue

        srcs = sources_from_config(conf)
        try:
            cands = run_lane(lane, sources=srcs,
                             per_query=int(conf.get("per_query", 8)))
        except Exception as e:
            log.warning("[%s] 发现失败：%s", name, str(e)[:160])
            summary.setdefault(name, {"found": 0, "new": 0,
                                      "skipped": 0, "errors": [str(e)[:80]]})
            continue

        added, skipped, errors = 0, 0, []
        for c in cands:
            if db.find_by_source_url(c.url):
                skipped += 1
                continue
            try:
                meta = extract_meta(c.url)
            except Exception as e:
                errors.append(f"{c.url}: {str(e)[:60]}")
                continue
            # 拿到真实元数据后再过一遍时长闸门（搜索结果的 duration 常为 0）
            if meta.duration and not (lane.min_duration <= meta.duration <= lane.max_duration):
                skipped += 1
                continue
            # 二次题材校验：用平台真实 caption 再确认一次
            # （搜索标题可能是频道自定义的，真实 caption 才反映内容）
            check = Candidate(url=c.url, title=c.title,
                              description=meta.caption or "", duration=meta.duration)
            ok, why = evaluate(check, lane)
            if not ok:
                skipped += 1
                log.debug("[%s] 元数据阶段丢弃 %s：%s", name, c.url, why)
                continue
            try:
                tid = db.insert_video(meta, target_platforms=[name],
                                      source_tag=f"auto:{c.source}")
            except Exception as e:
                errors.append(f"{c.url}: 入库失败 {str(e)[:50]}")
                continue
            if tid:
                added += 1
                log.info("[%s] 自动发现新素材 tid=%d %s（命中 %s）",
                         name, tid, c.title[:40], ",".join(check.matched_keywords[:3]))
            else:
                skipped += 1

        summary[name] = {"found": len(cands), "new": added,
                         "skipped": skipped, "errors": errors[:5]}
        if added:
            log.info("[%s] 自动发现完成：新增 %d 条（候选 %d，跳过 %d）",
                     name, added, len(cands), skipped)

    _last_run["all"] = time.time()
    return summary


def _discovery_due(s) -> bool:
    """是否到了下一轮发现时间（按各 lane 里最小的 interval_hours 算）。"""
    intervals = []
    for plat in s.platforms.values():
        conf = plat.discover or {}
        if conf.get("enabled"):
            try:
                intervals.append(float(conf.get("interval_hours", 6)))
            except (TypeError, ValueError):
                intervals.append(6.0)
    if not intervals:
        return False
    interval = min(intervals) * 3600
    return (time.time() - _last_run.get("all", 0)) >= interval


# ---------------------------------------------------------------------------
# 调度：跑一条 lane
# ---------------------------------------------------------------------------

def run_lane(lane: LaneConfig, sources: list[str] | None = None,
             per_query: int = 8) -> list[Candidate]:
    """按 lane 配置跑一轮发现，返回通过题材过滤的去重候选。"""
    sources = sources or ["youtube"]
    seen_urls: set[str] = set()
    picked: list[Candidate] = []

    for src in sources:
        fn = SOURCES.get(src)
        if not fn:
            log.warning("未知来源 %s，跳过", src)
            continue
        for q in lane.queries:
            for cand in fn(q, per_query):
                if cand.url in seen_urls:
                    continue
                ok, why = evaluate(cand, lane)
                if not ok:
                    log.debug("[%s] 丢弃 %s：%s", lane.name, cand.title[:40], why)
                    continue
                seen_urls.add(cand.url)
                picked.append(cand)
                if len(picked) >= lane.per_run:
                    return picked

    return picked

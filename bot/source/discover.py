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
    """返回 text 里命中的关键词（大小写不敏感，支持中英文）。

    防御非字符串项：config.yaml 里 `- shop:` 会被 YAML 解析成
    {'shop': None}，直接 .lower() 会抛 AttributeError，
    把整条发现链路带崩。这种情况跳过该项即可。
    """
    low = _norm_text(text)
    out: list[str] = []
    for k in keywords or []:
        if not isinstance(k, str) or not k:
            continue
        if _norm_text(k) in low:
            out.append(k)
    return out


def evaluate(cand: Candidate, lane: LaneConfig,
             allow_empty_hint: bool = False) -> tuple[bool, str]:
    """判断候选是否可进入该赛道。返回 (是否通过, 原因)。

    allow_empty_hint：搜索阶段的候选常常只有 URL、没有标题
    （ytsearch 偶发返回空 title）。此时不该在搜索阶段判死 ——
    真实信息要等 extract_meta 拿到。传 True 时「无题材证据」不拦，
    由元数据阶段（拿到真实 title/description 后）再做最终判断。
    """
    if cand.duration and not (lane.min_duration <= cand.duration <= lane.max_duration):
        return False, f"时长 {cand.duration:.0f}s 不在 {lane.min_duration:.0f}~{lane.max_duration:.0f}s"

    blocked = match_keywords(cand.hint_text, lane.block_any)
    if blocked:
        return False, f"命中排除词：{','.join(blocked)}"

    if lane.require_any:
        hit = match_keywords(cand.hint_text, lane.require_any)
        if not hit:
            if allow_empty_hint and not (cand.title or cand.description or "").strip():
                return True, "标题为空，留到元数据阶段判断"
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
    """IG 发现（白名单账号路径）。query 是账号名。

    hashtag / 关键词搜索走独立入口（discover_instagram_tag /
    discover_instagram_keyword），由 run_ig_search 调度 —— 两者语义不同，
    混在一个函数里靠字符串前缀猜格式太脆。
    """
    from pathlib import Path

    from .ig_discover import discover_instagram_profile
    from ..config import DATA_DIR

    items = discover_instagram_profile(
        query, limit=limit, cookie_dir=Path(DATA_DIR) / "cookies.d")
    return [Candidate(
        url=it["url"],
        title=f"@{query}",
        description="",
        source="instagram",
        channel=query,
    ) for it in items]


def run_ig_search(tags: list[str], keywords: list[str],
                  limit_per_entry: int = 24,
                  per_run: int = 10,
                  pages: int = 1,
                  db = None) -> list[Candidate]:
    """跑 IG hashtag + 关键词搜索，返回候选。

    2026-11-09 实测三种入口都可用（各拿到 24 条链接）：
        explore/tags/bedroomdecor              ✅
        explore/tags/床品（URL 编码）            ✅
        explore/search/keyword/?q=bedroom decor ✅

    为什么这条路比 YouTube 搜索值得做：
    - IG 上是「真实卖家/用户拍的使用场景」，YouTube 搜出来多是
      10~25 分钟测评教程或 Amazon 联盟推广
    - hashtag 天然带题材语义（#床品 下面不会有修眉教程）
    - 中文 hashtag 也能用（#床品 ✅），不依赖英文关键词
    """
    from pathlib import Path

    from .ig_discover import discover_instagram_keyword, discover_instagram_tag
    from ..config import DATA_DIR

    cdir = Path(DATA_DIR) / "cookies.d"
    seen: set[str] = set()
    picked: list[Candidate] = []

    for tag in tags:
        for it in discover_instagram_tag(tag, limit=limit_per_entry,
                                         cookie_dir=cdir, pages=pages):
            if it["url"] in seen:
                continue
            if db and db.find_by_source_url(it["url"]):
                continue
            seen.add(it["url"])
            picked.append(Candidate(url=it["url"], title=f"#{tag.lstrip('#')}",
                                    source="instagram_tag", channel=tag))
            if len(picked) >= per_run:
                return picked

    for kw in keywords:
        for it in discover_instagram_keyword(kw, limit=limit_per_entry,
                                             cookie_dir=cdir):
            if it["url"] in seen:
                continue
            if db and db.find_by_source_url(it["url"]):
                continue
            seen.add(it["url"])
            picked.append(Candidate(url=it["url"], title=f"kw:{kw}",
                                    source="instagram_kw", channel=kw))
            if len(picked) >= per_run:
                return picked

    return picked


SOURCES = {
    "youtube": discover_youtube,
    "instagram": discover_instagram,
}


# ---------------------------------------------------------------------------
# 来源：白名单账号（质量优先，与关键词搜索互补）
# ---------------------------------------------------------------------------

def discover_youtube_channel(handle: str, limit: int = 10) -> list[Candidate]:
    """拉某个 YouTube 频道的最新视频。

    handle 支持 @名字 或 UCxxx 频道 ID。用 yt-dlp 的频道列表，
    extract_flat 足够拿到 id/title/duration，不用逐条解析。
    """
    import yt_dlp

    if handle.startswith("UC") and len(handle) > 20:
        base = f"https://www.youtube.com/channel/{handle}"
    else:
        base = f"https://www.youtube.com/@{handle.lstrip('@')}"

    # 优先拉 Shorts 页签：本流水线发布的是竖屏短视频，频道主 feed 里
    # 大量是 10~30 分钟的长视频（实测 Studio McGee 5 条全在 631~1611 秒），
    # 拉回来也被时长闸门丢光。Shorts 页签才是真正对口的供给。
    # 若该频道没有 Shorts 页签，回退到普通 videos 页签。
    for url in (f"{base}/shorts", f"{base}/videos"):
        cands = _yt_tab(url, limit)
        if cands:
            return cands
    return []


def _yt_tab(url: str, limit: int) -> list[Candidate]:
    """拉一个 yt-dlp 能识别的列表页（频道 tab / 播放列表）。"""
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": max(1, limit),
        "skip_download": True,
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
    except Exception as e:
        log.debug("yt-dlp 列表页失败（%s）：%s", url, str(e)[:120])
        return []

    out: list[Candidate] = []
    for e in (info.get("entries") or []):
        vid = e.get("id") or ""
        if not vid:
            continue
        out.append(Candidate(
            url=f"https://www.youtube.com/watch?v={vid}",
            title=e.get("title") or "",
            description=e.get("description") or "",
            source="youtube_whitelist",
            channel=e.get("channel") or "",
            duration=float(e.get("duration") or 0),
        ))
    return out


WHITELIST_SOURCES = {
    "youtube": discover_youtube_channel,
    "instagram": discover_instagram,
}


def run_whitelist(lane: LaneConfig, platform: str,
                  per_account: int = 10,
                  per_run: int | None = None,
                  db = None) -> list[Candidate]:
    """从白名单账号拉候选。

    白名单是「已知质量好的源」，所以放宽题材要求：
    账号本身垂直，不必再用关键词卡一遍（否则会漏掉标题没写品类词的好素材）。
    但仍然执行 block_any —— 带货/美妆要挡。

    per_run 默认取 lane.per_run —— 布局配额应该只有一个来源，
    两处各写一份必然会出现「改了 lane 不生效」的困惑。
    """
    from .source_accounts import accounts_for

    if per_run is None:
        per_run = lane.per_run
    picked: list[Candidate] = []
    seen: set[str] = set()

    for platform_key, fn in WHITELIST_SOURCES.items():
        for acct in accounts_for(lane.name, platform_key):
            handle = str(acct.get("account") or "").strip()
            if not handle:
                continue
            # 每个账号只取「还差多少条」，避免头一个账号就把配额吃光 ——
            # 否则白名单只有第一个账号真正生效，后面几个形同虚设。
            need = per_run - len(picked)
            if need <= 0:
                return picked
            for cand in fn(handle, min(per_account, max(need, 1) * 2)):
                if cand.url in seen:
                    continue
                if db and db.find_by_source_url(cand.url):
                    continue
                # 时长闸门要和白名单一起用：实测 Studio McGee 这类家居频道
                # 全是 10~27 分钟的长视频（631~1611 秒），不过滤会白拉。
                if cand.duration and not (lane.min_duration <= cand.duration <= lane.max_duration):
                    log.debug("[%s] 白名单丢弃（时长 %.0fs）：%s",
                              lane.name, cand.duration, cand.title[:40])
                    continue
                blocked = match_keywords(cand.hint_text, lane.block_any)
                if blocked:
                    log.debug("[%s] 白名单丢弃（排除词 %s）：%s",
                              lane.name, ",".join(blocked), cand.title[:40])
                    continue
                seen.add(cand.url)
                picked.append(cand)
                if len(picked) >= per_run:
                    return picked
    return picked


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
    """配置里的 sources 列表，过滤掉不认识的来源。

    注意：显式写空列表 `sources: []` 表示「不要关键词搜索」，
    必须原样返回空 —— 不能回退成默认的 youtube。
    2026-11-09 踩过：用户要停 YouTube，但代码把空列表当成「没配置」
    自动补回 youtube，配置改了却不生效。
    只有**完全没写这个字段**（None）才用默认值。
    """
    raw = conf.get("sources")
    if raw is None:
        return ["youtube"]
    if not isinstance(raw, (list, tuple)):
        return ["youtube"]
    picked = [str(x).strip() for x in raw if str(x).strip()]
    return [s for s in picked if s in SOURCES]


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
        has_queries = bool(lane.queries)
        has_ig = bool(conf.get("ig_tags") or conf.get("ig_keywords"))
        from .source_accounts import accounts_for
        has_whitelist = bool(accounts_for(name, "instagram") or accounts_for(name, "youtube"))
        if not (has_queries or has_ig or has_whitelist):
            log.warning("[%s] 自动发现已启用但未配置任何寻源途径（无 queries/无 ig_tags/无白名单），跳过", name)
            continue

        srcs = sources_from_config(conf)
        try:
            # 三条供给线，按质量优先级：
            #   1. 白名单账号（人工确认过质量）
            #   2. IG hashtag / 关键词搜索（题材语义天然对齐）
            #   3. 其它来源（目前只剩停用中的 YouTube）
            cands = run_whitelist(
                lane, name,
                per_account=int(conf.get("whitelist_per_account", 10)),
                per_run=lane.per_run,
                db=db,
            )
            if len(cands) < lane.per_run:
                tags = [str(t) for t in (conf.get("ig_tags") or []) if str(t).strip()]
                kws = [str(k) for k in (conf.get("ig_keywords") or []) if str(k).strip()]
                if tags or kws:
                    cands += run_ig_search(
                        tags, kws,
                        limit_per_entry=int(conf.get("ig_limit_per_entry", 24)),
                        per_run=lane.per_run - len(cands),
                        pages=int(conf.get("ig_pages", 1)),
                        db=db,
                    )
            if len(cands) < lane.per_run and srcs:
                cands += run_lane(lane, sources=srcs,
                                  per_query=int(conf.get("per_query", 8)))
            # 合并后去重：白名单与搜索可能命中同一条
            deduped, seen = [], set()
            for c in cands:
                if c.url in seen:
                    continue
                seen.add(c.url)
                deduped.append(c)
            cands = deduped[:lane.per_run]
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
            # 二次题材校验：用平台真实元数据再确认一次。
            # 搜索标题可能是频道自定义的，真实元数据才反映内容。
            # 注意必须同时带上 meta.title：很多短视频只有标题、没有 description
            # （实测 2026-11-09：一条 cozy bedroom 视频 description 为空，
            # 只靠 caption 会误判成「无题材证据」）。
            check = Candidate(url=c.url,
                              title=f"{c.title} {meta.title or ''}".strip(),
                              description=meta.caption or "",
                              duration=meta.duration)
            is_whitelist = "whitelist" in c.source or c.source == "instagram"
            if is_whitelist:
                # 白名单账号已由人工/爆款晋级确认赛道垂直度，不强制要求 caption 包含品类词；
                # 但仍然执行 block_any 排除（防止发广告/美妆/无关翻车）。
                blocked = match_keywords(check.hint_text, lane.block_any)
                if blocked:
                    skipped += 1
                    log.debug("[%s] 白名单元数据阶段丢弃（命中排除词 %s）：%s",
                              name, ",".join(blocked), c.url)
                    continue
                if not check.matched_keywords:
                    check.matched_keywords = ["whitelist"]
            else:
                ok, why = evaluate(check, lane)
                if not ok:
                    skipped += 1
                    log.debug("[%s] 元数据阶段丢弃 %s：%s", name, c.url, why)
                    continue
                # 有 require_any 的 lane 必须能证明题材对口。
                # 若搜索标题和真实 caption 都是空的（实测有这种视频：caption 只有
                # 外链或被平台清空），说明拿不到任何题材证据 —— 宁可不要，
                # 也不要把「不知道是什么」的内容发出去。B 号漂移就是这样开始的。
                if lane.require_any and not check.matched_keywords:
                    skipped += 1
                    log.debug("[%s] 无题材证据（caption 为空）丢弃 %s", name, c.url)
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
                # caption 为空的视频在流水线里拿不到任何文案输入
                # （文案生成靠 caption + 转录，两者都空就只能靠画面探针）。
                # 这里把标题补进 caption，至少给下游一个语义线索。
                if not (meta.caption or "").strip() and (meta.title or "").strip():
                    try:
                        db.update(tid, caption=meta.title)
                    except Exception:
                        pass
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
    """按 lane 配置跑一轮发现，返回通过题材过滤的去重候选。

    sources 显式传空列表 = 不跑关键词搜索（只走白名单）。
    只有 None 才回退到默认 youtube —— 同 sources_from_config 的理由。
    """
    if sources is None:
        sources = ["youtube"]
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
                # 允许搜索阶段标题为空：真实信息在元数据阶段才拿得到，
                # 那里会用 require_any 做最终闸门（见 run_discovery）。
                ok, why = evaluate(cand, lane, allow_empty_hint=True)
                if not ok:
                    log.debug("[%s] 丢弃 %s：%s", lane.name, cand.title[:40], why)
                    continue
                seen_urls.add(cand.url)
                picked.append(cand)
                if len(picked) >= lane.per_run:
                    return picked

    return picked

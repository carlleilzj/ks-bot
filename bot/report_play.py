"""分区 / 话题维度的播放效果报表。

解决什么问题
------------
现在只有一个「按播放量倒序」的列表（--stats-report），它能回答
「哪条最好」，但回答不了「**哪类内容**最好」——而这才是选题决策需要的。

本模块把作品按 分区（category）和 话题（tags）聚合，给出：
    n        样本数
    中位数   抗极值（播放量分布极度偏态，均值会被爆款拉飞）
    均值/最高 P25/P75  看分布形状
    覆盖率   该组有播放数据的比例（数据不全时必须标出来，否则误判）

为什么用中位数而不是均值
------------------------
平台播放量是长尾分布：一条 33 万播放会把均值拉到 1 万，
而实际上 90% 的作品只有几百播放。用均值比较两个赛道会得到
完全错误的结论（见 B 号真实数据：均值 10237，中位数 1286）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from statistics import median

log = logging.getLogger(__name__)

# 样本数低于此值不给「建议」，只列数据。
# 依据：n<5 时中位数完全可能被单条爆款/单条零播左右，据此调整选题会误导。
MIN_SAMPLE_FOR_ADVICE = 5


@dataclass
class GroupStat:
    """一个分组（某分区 / 某话题）的统计结果。"""
    name: str
    plays: list[int] = field(default_factory=list)
    # 该组总作品数（含没有播放数据的），用于算覆盖率
    total_works: int = 0

    @property
    def n(self) -> int:
        """有播放数据的样本数。"""
        return len(self.plays)

    @property
    def coverage(self) -> float:
        """有播放数据占比（0~1）。"""
        if not self.total_works:
            return 0.0
        return self.n / self.total_works

    @property
    def median(self) -> float:
        return float(median(self.plays)) if self.plays else 0.0

    @property
    def mean(self) -> float:
        return sum(self.plays) / self.n if self.plays else 0.0

    @property
    def p25(self) -> float:
        return _percentile(self.plays, 0.25)

    @property
    def p75(self) -> float:
        return _percentile(self.plays, 0.75)

    @property
    def max_play(self) -> int:
        return max(self.plays) if self.plays else 0

    @property
    def zero_play(self) -> int:
        """播放量为 0 的作品数（重要信号：可能被限流或内容不推荐）。"""
        return sum(1 for p in self.plays if p == 0)

    @property
    def reliable(self) -> bool:
        """样本量是否足以支撑判断。"""
        return self.n >= MIN_SAMPLE_FOR_ADVICE


def _percentile(values: list[int], q: float) -> float:
    """线性插值分位数（不引入 numpy）。"""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


# ---------------------------------------------------------------------------
# 从 DB 读取作品行
# ---------------------------------------------------------------------------

@dataclass
class WorkRow:
    """一条作品的分析视图。"""
    platform: str
    title: str
    play: int
    category: str
    tags: list[str]
    published_at: str
    # 测量时距发布已过多少小时。None = 没有播放数据或无 stats_at。
    # 为什么必须带着它：play 是「某一时刻的快照」，不是「作品的最终成绩」。
    # 不同作品的快照年龄可能差 300 小时，直接比会得出完全错误的结论
    # （2026-11-09 实测：原始比 3180→146 是 22 倍，同年龄对齐后只有 5.4 倍）。
    age_hours: float | None = None


# 一条作品要「成熟」需要多少小时的曝光。
# 依据：实测播放量在发布后 72 小时内基本完成主要增长，之后是长尾
# （见 2026-11-09 分析：36~72h 中位 2109，72~120h 2403，120h 后骤降到 309）。
MATURE_HOURS = 72.0

# 年龄跨度超过这个倍数时，才提示「不可直接比较」。
# 取 3：年龄 24h 与 72h 混在一起会明显失真，而 60h 与 72h 基本可比。
AGE_SPREAD_WARN_RATIO = 3.0


def load_works(db, platform: str, days: int | None = None,
               min_age_hours: float | None = None) -> list[WorkRow]:
    """取出某平台已发布作品的文案字段与播放数据。

    只取 PUBLISHED 的 job；play_count 可能为 NULL（回流未匹配到），
    这类作品计入 total_works 但不计入样本（覆盖率据此计算）。

    min_age_hours：只要「测量时已满 N 小时」的样本。None = 不过滤。
    """
    sql = """
        SELECT j.platform, t.title, t.play_count, t.copy_json, j.published_at,
               t.stats_at
        FROM publish_jobs j JOIN tasks t ON t.id = j.task_id
        WHERE j.state = 'PUBLISHED' AND j.platform = ?
    """
    params: list = [platform]
    if days:
        sql += " AND j.published_at >= datetime('now', ?)"
        params.append(f"-{int(days)} days")
    sql += " ORDER BY j.published_at DESC"

    with db._lock:
        rows = db.conn.execute(sql, params).fetchall()

    out: list[WorkRow] = []
    for r in rows:
        copy = {}
        try:
            copy = (json.loads(r["copy_json"] or "{}").get(platform) or {})
        except Exception:
            copy = {}
        category = str(copy.get("category") or "").strip()
        tags = [str(t) for t in (copy.get("tags") or []) if str(t).strip()]
        age = _age_hours(r["published_at"], r["stats_at"])
        if min_age_hours is not None and (age is None or age < min_age_hours):
            continue
        out.append(WorkRow(
            platform=platform,
            title=r["title"] or "",
            play=int(r["play_count"]) if r["play_count"] is not None else -1,
            category=category,
            tags=tags,
            published_at=r["published_at"] or "",
            age_hours=age,
        ))
    return out


def _age_hours(published_at: str | None, stats_at: str | None) -> float | None:
    """测量时刻距发布时刻的小时数。任一缺失返回 None。"""
    if not published_at or not stats_at:
        return None
    try:
        from datetime import datetime
        pub = datetime.fromisoformat(str(published_at).replace("Z", ""))
        st = datetime.fromisoformat(str(stats_at).replace("Z", ""))
    except Exception:
        return None
    delta = (st - pub).total_seconds() / 3600
    return delta if delta >= 0 else None


def age_breakdown(works: list[WorkRow]) -> list[tuple[str, int, float]]:
    """按「测量年龄」分桶统计中位播放，用于揭示测量偏差。

    为什么这件事必须显式呈现
    ----------------------
    播放量是长尾增长，6 小时测和 7 天测能差几十倍。如果报表只给一个
    总体中位数，读者会把它当成「账号水平」，而实际上它是
    「不同年龄样本的混合值」——年龄越新的样本越多，总体中位就越低。

    2026-11-09 的真实教训：A 号最近 45 条作品的播放量全部在同一秒
    （11-09T16:47）批量回填，于是 11-09 发布的视频只测了 0~6 小时。
    把日期当横轴做趋势，必然看到一条「暴跌曲线」——那是测量节奏，
    不是账号表现。
    """
    buckets = [(0, 12), (12, 36), (36, 72), (72, 120),
               (120, 180), (180, 240), (240, 400), (400, 10**9)]
    out: list[tuple[str, int, float]] = []
    for lo, hi in buckets:
        v = [w.play for w in works
             if w.play >= 0 and w.age_hours is not None
             and lo <= w.age_hours < hi]
        if v:
            label = f"{lo}~{hi}h" if hi < 10**9 else f">{lo}h"
            out.append((label, len(v), float(median(v))))
    return out


def age_spread(works: list[WorkRow]) -> tuple[float, float, bool]:
    """样本的年龄跨度，以及是否大到不可直接比较。

    返回 (最小小时, 最大小时, 是否需要警告)。
    """
    ages = [w.age_hours for w in works
            if w.play >= 0 and w.age_hours is not None]
    if not ages:
        return (0.0, 0.0, False)
    lo, hi = min(ages), max(ages)
    warn = lo > 0 and hi / lo >= AGE_SPREAD_WARN_RATIO and hi - lo >= 24
    return (lo, hi, warn)


# ---------------------------------------------------------------------------
# 分层对比：在每个测量年龄桶内部比较「早期 vs 近期」
# ---------------------------------------------------------------------------

@dataclass
class StratumCompare:
    """某个测量年龄桶内，「分界前」与「分界后」的中位播放对比。"""
    age_label: str
    age_lo: float
    age_hi: float
    before_median: float
    before_n: int
    after_median: float
    after_n: int

    @property
    def ratio(self) -> float:
        """after / before。<1 表示近期更低。"""
        return self.after_median / self.before_median if self.before_median > 0 else 0.0

    @property
    def comparable(self) -> bool:
        return self.before_n >= 3 and self.after_n >= 3


def stratified_compare(works: list[WorkRow], cut_date: str,
                       min_age_hours: float = 12.0) -> list[StratumCompare]:
    """在**同一测量年龄桶内**比较分界日前后，剥离测量偏差后的真实变化。

    为什么这是正解
    -------------
    单点对齐（align_by_age）要求每条作品都有「刚好 72 小时」那次测量。
    但抓取是每 6 小时一轮、且只覆盖管理页首屏（约最近 60 条），
    老作品的第一条快照往往就已经是 200+ 小时——单点对齐会丢掉大半样本。

    分层对比放宽了这个要求：只在「年龄相近」的作品之间比。
    年龄 200h 的老作品与年龄 200h 的新作品，仍然可比；
    年龄 200h 与年龄 20h 的不可比，但它们落在不同的层，不会互相污染。

    这正好对应 2026-11-09 手工分析时的做法：
    锁定 72~180h 窗口后，B 号拐点从「22 倍」修正为「5.4 倍」。

    min_age_hours：低于此年龄的样本不参与（预热期波动太大）。
    """
    buckets = [(12, 36), (36, 72), (72, 120), (120, 180),
               (180, 240), (240, 300), (300, 400), (400, 10**9)]
    out: list[StratumCompare] = []
    for lo, hi in buckets:
        if lo < min_age_hours:
            continue
        chunk = [w for w in works
                 if w.play >= 0 and w.age_hours is not None
                 and lo <= w.age_hours < hi]
        before = [w.play for w in chunk if (w.published_at or "")[:10] < cut_date]
        after = [w.play for w in chunk if (w.published_at or "")[:10] >= cut_date]
        if not before or not after:
            continue
        label = f"{int(lo)}~{int(hi)}h" if hi < 10**9 else f">{int(lo)}h"
        out.append(StratumCompare(
            age_label=label, age_lo=lo, age_hi=hi,
            before_median=float(median(before)), before_n=len(before),
            after_median=float(median(after)), after_n=len(after),
        ))
    return out


def render_stratified(rows: list[StratumCompare], cut_date: str) -> list[str]:
    """渲染分层对比。"""
    lines: list[str] = []
    lines.append(f"\n【分层对比】同一测量年龄内，{cut_date} 前后各是什么水平")
    lines.append("-" * 78)
    if not rows:
        lines.append("  （每个年龄层都缺一侧样本，无法分层对比）")
        return lines
    lines.append(f"{'年龄层':>12} {'前n':>4} {'前中位':>9} "
                 f"{'后n':>4} {'后中位':>9} {'倍数':>7}")
    lines.append("-" * 78)
    for r in rows:
        if not r.comparable:
            lines.append(f"{r.age_label:>12} {r.before_n:>4} {r.before_median:>9.0f} "
                         f"{r.after_n:>4} {r.after_median:>9.0f}   （样本少）")
            continue
        arrow = "↓" if r.ratio < 1 else ("↑" if r.ratio > 1 else "=")
        factor = (1 / r.ratio if 0 < r.ratio < 1 else r.ratio)
        lines.append(f"{r.age_label:>12} {r.before_n:>4} {r.before_median:>9.0f} "
                     f"{r.after_n:>4} {r.after_median:>9.0f} "
                     f"{arrow}{factor:>5.1f}x")
    lines.append("-" * 78)
    good = [r for r in rows if r.comparable]
    if good:
        # 各层方向一致才有说服力：如果有的层跌有的层涨，说明差异不是时间造成的
        downs = sum(1 for r in good if r.ratio < 0.6)
        ups = sum(1 for r in good if r.ratio > 1.5)
        if downs and not ups:
            lines.append(f"  ✅ {downs} 个年龄层一致下跌 —— 排除测量偏差后，下滑是真实的")
        elif ups and not downs:
            lines.append(f"  ✅ {ups} 个年龄层一致上涨")
        elif downs and ups:
            lines.append("  ⚠️ 各层方向不一致 —— 更像是样本噪声，不宜据此下结论")
        else:
            lines.append("  ✅ 各层基本持平 —— 之前看到的暴跌很可能是测量偏差")
    return lines

# 目标年龄附近允许多大偏差。取 ±36h：
# 太窄会大量丢样本（测量间隔 6h，但作品可能错过窗口）；
# 太宽又会重新掺入增长差异（36~72h 与 72~108h 的中位已相差 1.3 倍）。
AGE_ALIGN_TOLERANCE_HOURS = 36.0


def align_by_age(db, platform: str, target_age_hours: float = MATURE_HOURS,
                 tolerance_hours: float = AGE_ALIGN_TOLERANCE_HOURS,
                 days: int | None = None) -> tuple[list[WorkRow], str]:
    """每条作品取「最接近 target_age_hours」的那次快照，使样本可比。

    为什么需要这一步
    ---------------
    播放量随时间增长。要比较「最近的内容 vs 之前的内容」，必须保证
    两边测的是同一时刻的成绩，否则测到的是「年龄差」而不是「内容差」。

    2026-11-09 实测：不控年龄时 B 号 11-03 前后是 3180→146（22 倍），
    锁定在 120~180h 后是 1923→356（5.4 倍）。差的那 4 倍纯属测量偏差。

    返回 (works, 说明文本)。
    说明文本会写清实际用了多少次快照、多少条作品没有合适的快照，
    因为「悄悄丢掉一半样本」是最容易骗过自己的做法。
    """
    snaps = db.play_snapshots(platform=platform)
    if not snaps:
        return [], "（无播放快照历史，无法按年龄对齐。跑一次 --fetch-stats 后即可累积）"

    # 每 (task) 取最接近目标年龄的一条
    best: dict[int, dict] = {}
    for s in snaps:
        age = s.get("age_hours")
        if age is None:
            continue
        diff = abs(float(age) - target_age_hours)
        if diff > tolerance_hours:
            continue
        tid = int(s["task_id"])
        cur = best.get(tid)
        if cur is None or diff < abs(float(cur["age_hours"]) - target_age_hours):
            best[tid] = s

    if not best:
        return [], (f"（没有一条快照落在 {target_age_hours:.0f}±"
                    f"{tolerance_hours:.0f}h 窗口内，无法对齐）")

    # 补上文案/分区/tags 与发布日
    placeholders = ",".join("?" * len(best))
    sql = f"""
        SELECT j.platform, t.id, t.title, t.copy_json, j.published_at
        FROM publish_jobs j JOIN tasks t ON t.id = j.task_id
        WHERE j.state = 'PUBLISHED' AND j.platform = ?
          AND t.id IN ({placeholders})
    """
    params: list = [platform, *best.keys()]
    if days:
        sql += " AND j.published_at >= datetime('now', ?)"
        params.append(f"-{int(days)} days")
    with db._lock:
        rows = db.conn.execute(sql, params).fetchall()

    out: list[WorkRow] = []
    for r in rows:
        s = best.get(int(r["id"]))
        if not s:
            continue
        copy = {}
        try:
            copy = (json.loads(r["copy_json"] or "{}").get(platform) or {})
        except Exception:
            copy = {}
        out.append(WorkRow(
            platform=platform,
            title=r["title"] or "",
            play=int(s["play_count"]),
            category=str(copy.get("category") or "").strip(),
            tags=[str(t) for t in (copy.get("tags") or []) if str(t).strip()],
            published_at=r["published_at"] or "",
            age_hours=float(s["age_hours"]),
        ))

    out.sort(key=lambda w: w.published_at)
    total_tasks = len({int(s["task_id"]) for s in snaps})
    note = (f"（按年龄对齐：目标 {target_age_hours:.0f}h ±{tolerance_hours:.0f}h；"
            f"命中 {len(out)} 条 / 有快照的 {total_tasks} 条作品）")
    return out, note


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------

def group_by_category(works: list[WorkRow]) -> list[GroupStat]:
    """按分区聚合，按中位播放降序。"""
    groups: dict[str, GroupStat] = {}
    for w in works:
        name = w.category or "(未分区)"
        g = groups.setdefault(name, GroupStat(name=name))
        g.total_works += 1
        if w.play >= 0:
            g.plays.append(w.play)
    return sorted(groups.values(), key=lambda g: (-g.median, -g.n))


def group_by_tag(works: list[WorkRow], min_works: int = 2) -> list[GroupStat]:
    """按话题聚合，按中位播放降序。

    min_works: 至少出现在多少个作品里才纳入（避免单次出现的长尾词干扰）。
    """
    groups: dict[str, GroupStat] = {}
    for w in works:
        # 一条作品可能有多个话题；每个话题都计入，但 total_works 按话题计
        for tag in dict.fromkeys(w.tags):
            g = groups.setdefault(tag, GroupStat(name=tag))
            g.total_works += 1
            if w.play >= 0:
                g.plays.append(w.play)
    picked = [g for g in groups.values() if g.total_works >= min_works]
    return sorted(picked, key=lambda g: (-g.median, -g.n))


def overall(works: list[WorkRow]) -> GroupStat:
    """全平台汇总（用于对比各分组的相对水平）。"""
    g = GroupStat(name="全部")
    g.total_works = len(works)
    g.plays = [w.play for w in works if w.play >= 0]
    return g


# ---------------------------------------------------------------------------
# 逐日趋势与拐点检测
# ---------------------------------------------------------------------------

@dataclass
class DayStat:
    """单日统计。"""
    date: str
    plays: list[int] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.plays)

    @property
    def median(self) -> float:
        return float(median(self.plays)) if self.plays else 0.0

    @property
    def total(self) -> int:
        return sum(self.plays)


@dataclass
class ChangePoint:
    """检测到的拐点。"""
    date: str
    before_median: float
    after_median: float
    before_n: int
    after_n: int
    ratio: float          # after / before，<1 表示下跌
    # 置信度：样本量是否足够
    confident: bool


def daily_stats(works: list[WorkRow]) -> list[DayStat]:
    """按发布日聚合，日期升序。

    只统计有播放数据的作品；没有播放数据的日期不出现
    （避免把「数据没回流」误显示成「那天播放为 0」）。
    """
    by_day: dict[str, list[int]] = {}
    for w in works:
        if w.play < 0:
            continue
        day = (w.published_at or "")[:10]
        if not day:
            continue
        by_day.setdefault(day, []).append(w.play)
    return [DayStat(date=d, plays=sorted(p))
            for d, p in sorted(by_day.items())]


def detect_change_points(days: list[DayStat],
                         min_samples_per_side: int = 4,
                         ratio_threshold: float = 0.35) -> list[ChangePoint]:
    """扫描拐点：某个日期之后中位数相对之前骤降/骤升。

    为什么用「前后中位数比」而不是斜率：
    播放量是长尾分布 + 日样本少（每天 2~6 条），线性回归会被单条爆款
    彻底带偏。中位数比值对极值免疫。

    参数：
        min_samples_per_side: 拐点两侧各自至少多少条样本，否则不判
        ratio_threshold: 比值低于此值（下跌）或高于倒数（上涨）才报
                         0.35 意味着「掉到原来的 1/3 以下」

    返回按严重程度排序的拐点列表。
    """
    if len(days) < min_samples_per_side:
        return []

    points: list[ChangePoint] = []
    # 扫描所有可能的分割点；两侧各自用**样本条数**判断是否足够，
    # 而不是用天数 —— 否则「4 天数据、min=4」会让循环范围为空，
    # 明明样本够却检不出拐点（2026-11-09 踩过）。
    for i in range(1, len(days)):
        before = [p for d in days[:i] for p in d.plays]
        after = [p for d in days[i:] for p in d.plays]
        if len(before) < min_samples_per_side or len(after) < min_samples_per_side:
            continue

        b_med = float(median(before))
        a_med = float(median(after))
        if b_med <= 0:
            continue

        ratio = a_med / b_med
        # 只报显著变化：跌到 1/3 以下，或涨到 3 倍以上
        if ratio <= ratio_threshold or ratio >= 1 / ratio_threshold:
            points.append(ChangePoint(
                date=days[i].date,
                before_median=b_med,
                after_median=a_med,
                before_n=len(before),
                after_n=len(after),
                ratio=ratio,
                confident=(len(before) >= 10 and len(after) >= 10),
            ))

    if not points:
        return []

    # 合并同一段趋势的相邻拐点。
    # 问题：长期下行趋势会在每个分割点都触发检测，A 号真实数据报出 8 个
    # 「拐点」（9-15/9-19/9-24/9-30/10-18/10-24/10-29/11-08），
    # 但它们指向同一段衰退，逐个列出只会淹没真正的突变。
    #
    # 合并方式：同方向、间隔 <= max_span_days 的连续拐点视为一段趋势，
    # 合成一条 —— **date 取起点**（趋势从哪天开始，这是最有用的信息），
    # before 取首段的基线，after 取末段的最新水平。
    spans: list[ChangePoint] = []
    for p in points:
        if spans:
            prev = spans[-1]
            same_direction = (p.ratio < 1) == (prev.ratio < 1)
            if same_direction and _days_close(prev.date, p.date, max_gap=30):
                # 扩展这段趋势：保持起点与前基线，更新终点与最新水平。
                # before_n/after_n 取合并后两端的实际样本数 —— 直接用 p 的
                # 会缩成最后一个分割点附近的小样本，把 confident 判成 False
                # （真实数据里明明有 100+ 条样本却报「样本偏少」）。
                spans[-1] = ChangePoint(
                    date=prev.date,
                    before_median=prev.before_median,
                    after_median=p.after_median,
                    before_n=prev.before_n,
                    after_n=p.after_n,
                    ratio=(p.after_median / prev.before_median
                           if prev.before_median > 0 else 0.0),
                    confident=(prev.before_n >= 10 and p.after_n >= 10),
                )
                continue
        spans.append(p)

    # 按偏离程度排序（最严重的在前）
    # 合并后重算 before_n / after_n：用最终确定的 date 重新切分，
    # 否则会沿用最后一个分割点的样本数（那一段往往只有几天数据），
    # 把明明有上百条样本的趋势误标成「样本偏少」。
    day_map = {d.date: d for d in days}
    recalculated: list[ChangePoint] = []
    for sp in spans:
        idx = next((i for i, d in enumerate(days) if d.date >= sp.date), None)
        if idx is None:
            recalculated.append(sp)
            continue
        b = sum((len(d.plays) for d in days[:idx]), 0)
        a = sum((len(d.plays) for d in days[idx:]), 0)
        recalculated.append(ChangePoint(
            date=sp.date,
            before_median=sp.before_median,
            after_median=sp.after_median,
            before_n=b,
            after_n=a,
            ratio=sp.ratio,
            confident=(b >= 10 and a >= 10),
        ))

    return sorted(recalculated, key=lambda p: abs(p.ratio - 1), reverse=True)


def _days_close(a: str, b: str, max_gap: int = 3) -> bool:
    """两个 YYYY-MM-DD 相差是否在 max_gap 天内。"""
    from datetime import date
    try:
        da = date.fromisoformat(a)
        db_ = date.fromisoformat(b)
    except ValueError:
        return False
    return abs((db_ - da).days) <= max_gap


def render_trend(days: list[DayStat], points: list[ChangePoint],
                 bar_max: int = 40) -> list[str]:
    """渲染逐日趋势（含简易条形图与拐点标注）。"""
    lines: list[str] = []
    lines.append("\n【逐日趋势】（按发布日中位播放）")
    lines.append("-" * 78)
    if not days:
        lines.append("  （无播放数据）")
        return lines

    peak = max((d.median for d in days), default=1) or 1
    point_dates = {p.date for p in points}

    header = f"{'日期':<12} {'条数':>4} {'中位':>7} {'总播放':>9}  "
    lines.append(header + "分布")
    lines.append("-" * 78)

    for d in days:
        bar_len = int(d.median / peak * bar_max)
        bar = "█" * bar_len
        mark = "  ⚠️ 拐点" if d.date in point_dates else ""
        lines.append(
            f"{d.date:<12} {d.n:>4} {d.median:>7.0f} {d.total:>9}  {bar}{mark}"
        )
    lines.append("-" * 78)
    return lines


def render_change_points(points: list[ChangePoint]) -> list[str]:
    """渲染拐点结论。"""
    lines: list[str] = []
    if not points:
        lines.append("\n✅ 未检测到显著拐点（各阶段中位播放稳定）")
        return lines

    lines.append("\n⚠️ 检测到播放量拐点：")
    for p in points:
        direction = "下跌" if p.ratio < 1 else "上涨"
        factor = 1 / p.ratio if p.ratio < 1 else p.ratio
        conf = "" if p.confident else "（样本偏少，需谨慎）"
        lines.append(
            f"  · {p.date} 起{direction} {factor:.1f} 倍："
            f"{p.before_median:.0f} → {p.after_median:.0f}"
            f"（前 {p.before_n} 条 / 后 {p.after_n} 条）{conf}"
        )
    lines.append(
        "\n  排查方向（按可能性排序）：\n"
        "    1. 账号是否被限流/降权 —— 查创作平台通知与作品状态\n"
        "    2. 平台大盘是否同期下行 —— 看其他账号是否同跌\n"
        "    3. 素材质量是否变化 —— 对比拐点前后的视频\n"
        "  ⚠️ 拐点是**时间信号**，不要归因到单个话题或标题风格上\n"
        "     （容易与话题变更共线，见 2026-10-29 归因失败案例）")
    return lines



# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def render_table(stats: list[GroupStat], title: str, top: int = 20) -> list[str]:
    """把统计结果渲染成等宽文本表格。"""
    lines: list[str] = []
    lines.append(f"\n{title}")
    lines.append("-" * 78)
    header = (f"{'名称':<16} {'样本':>5} {'覆盖':>6} {'中位':>8} "
              f"{'P25':>7} {'P75':>7} {'均值':>9} {'最高':>9} {'零播':>5}")
    lines.append(header)
    lines.append("-" * 78)

    shown = [g for g in stats if g.n > 0][:top]
    if not shown:
        lines.append("  （无有播放数据的样本）")
        return lines

    for g in shown:
        name = g.name[:16]
        cov = f"{g.coverage * 100:.0f}%"
        star = "" if g.reliable else " *"
        lines.append(
            f"{name:<16} {g.n:>5} {cov:>6} {g.median:>8.0f} "
            f"{g.p25:>7.0f} {g.p75:>7.0f} {g.mean:>9.0f} {g.max_play:>9} "
            f"{g.zero_play:>5}{star}"
        )
    lines.append("-" * 78)
    unreliable = [g for g in shown if not g.reliable]
    if unreliable:
        lines.append(
            f"  * 样本 <{MIN_SAMPLE_FOR_ADVICE} 条，中位数不稳定，不建议据此定策略")
    return lines


def render_report(works: list[WorkRow], platform: str, top: int = 20,
                  show_trend: bool = True, mature_only: bool = False,
                  extra_note: str = "", cut_date: str = "") -> str:
    """完整报表文本。"""
    total = overall(works)
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(f" {platform} 分区 / 话题 播放效果报表")
    lines.append("=" * 78)

    missing = total.total_works - total.n
    lines.append(
        "\n样本：{0} 条有播放数据 / 共 {1} 条已发布（覆盖 {2:.0f}%）".format(
            total.n, total.total_works, total.coverage * 100))
    if missing:
        lines.append(
            f"  ⚠️ {missing} 条缺播放数据（回流未匹配），"
            f"各组「覆盖」列已标注，低于 60% 的结论要谨慎")

    # 测量年龄诊断必须放在最前：它决定后面所有数字能不能直接比。
    lo, hi, spread_warn = age_spread(works)
    if mature_only:
        lines.append(f"\n样本已按成熟度过滤：仅含发布满 {MATURE_HOURS:.0f} 小时的测量")
        if extra_note:
            lines.append(f"  {extra_note}")
    elif spread_warn:
        lines.append("")
        lines.append("⚠️ 测量年龄不一致 —— 下面的日趋势与分组统计都不可直接比较：")
        lines.append(f"    样本的「发布→测量」时长从 {lo:.0f}h 到 {hi:.0f}h，"
                     f"相差 {hi / max(1.0, lo):.0f} 倍。")
        lines.append("    播放量是长尾增长，6 小时测和 7 天测能差几十倍，"
                     "按发布日期做趋势会得到假的暴跌。")
        lines.append(f"    解决：加 --mature-only 只看发布满 {MATURE_HOURS:.0f} 小时的样本。")
        breakdown = age_breakdown(works)
        if breakdown:
            lines.append("")
            lines.append("    【按测量年龄】中位播放（同样内容，测得越久数字越大）")
            for label, n, med_v in breakdown:
                lines.append(f"      {label:>10}  n={n:>3}  中位 {med_v:>8.0f}")

    if total.n:
        lines.append(
            f"\n整体：中位 {total.median:.0f} / 均值 {total.mean:.0f} / "
            f"最高 {total.max_play} / 零播 {total.zero_play} 条")

    # 趋势放在最前：如果检测到拐点，后面的分组结论都不可靠
    # （拐点前后的数据混在一起比，会得出完全错误的「哪类内容好」）
    if show_trend:
        days = daily_stats(works)
        points = detect_change_points(days)
        lines.extend(render_change_points(points))
        if points:
            lines.append(
                "\n⚠️ 由于存在拐点，下面的分组统计混了「拐点前」与「拐点后」的数据，"
                "\n   结论只反映整体平均，**不能代表当前内容效果**。"
                "\n   建议配合 --report-days N 只看拐点之后的区间。")
        lines.extend(render_trend(days, points, bar_max=36))
        # 分层对比放在趋势后面：趋势会因测量偏差失真，分层对比能校正
        if cut_date:
            lines.extend(render_stratified(
                stratified_compare(works, cut_date), cut_date))
        elif points:
            lines.extend(render_stratified(
                stratified_compare(works, points[0].date), points[0].date))

    lines.extend(render_table(group_by_category(works), "【按分区】", top=top))
    lines.extend(render_table(group_by_tag(works), "【按话题】", top=top))

    lines.append("\n" + "=" * 78)
    lines.append("读法：")
    lines.append("  · 先看【逐日趋势】—— 有拐点时，分组统计会误导（前后数据混算）")
    lines.append("  · 看**中位**不看均值 —— 播放是长尾分布，一条爆款能把均值拉飞")
    lines.append("  · P25/P75 看稳定性：区间越窄，这类内容表现越可预期")
    lines.append("  · 零播数 >0 要警惕：可能是内容不推荐，也可能是被限流")
    lines.append("  · 覆盖 <60% 时，该组结论可能只代表「能回流出数据的那部分」")
    lines.append("  · 测量年龄不一致时，日趋势会显示假的暴跌 —— 用 --mature-only")
    lines.append("=" * 78)
    return "\n".join(lines)

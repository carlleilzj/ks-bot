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


def load_works(db, platform: str, days: int | None = None) -> list[WorkRow]:
    """取出某平台已发布作品的文案字段与播放数据。

    只取 PUBLISHED 的 job；play_count 可能为 NULL（回流未匹配到），
    这类作品计入 total_works 但不计入样本（覆盖率据此计算）。
    """
    sql = """
        SELECT j.platform, t.title, t.play_count, t.copy_json, j.published_at
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
        out.append(WorkRow(
            platform=platform,
            title=r["title"] or "",
            play=int(r["play_count"]) if r["play_count"] is not None else -1,
            category=category,
            tags=tags,
            published_at=r["published_at"] or "",
        ))
    return out


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


def render_report(works: list[WorkRow], platform: str, top: int = 20) -> str:
    """完整报表文本。"""
    total = overall(works)
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(f" {platform} 分区 / 话题 播放效果报表")
    lines.append("=" * 78)

    missing = total.total_works - total.n
    lines.append(
        f"\n样本：{total.n} 条有播放数据 / 共 {total.total_works} 条已发布"
        f"（覆盖 {total.coverage * 100:.0f}%）")
    if missing:
        lines.append(
            f"  ⚠️ {missing} 条缺播放数据（回流未匹配），"
            f"各组「覆盖」列已标注，低于 60% 的结论要谨慎")

    if total.n:
        lines.append(
            f"\n整体：中位 {total.median:.0f} / 均值 {total.mean:.0f} / "
            f"最高 {total.max_play} / 零播 {total.zero_play} 条")

    lines.extend(render_table(group_by_category(works), "【按分区】", top=top))
    lines.extend(render_table(group_by_tag(works), "【按话题】", top=top))

    lines.append("\n" + "=" * 78)
    lines.append("读法：")
    lines.append("  · 看**中位**不看均值 —— 播放是长尾分布，一条爆款能把均值拉飞")
    lines.append("  · P25/P75 看稳定性：区间越窄，这类内容表现越可预期")
    lines.append("  · 零播数 >0 要警惕：可能是内容不推荐，也可能是被限流")
    lines.append("  · 覆盖 <60% 时，该组结论可能只代表「能回流出数据的那部分」")
    lines.append("=" * 78)
    return "\n".join(lines)

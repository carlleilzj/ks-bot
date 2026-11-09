"""内容主题漂移检测：发现「最近的素材/选题和以前不一样了」。

为什么需要它
------------
2026-11-09 排查 B 号播放暴跌时，靠手写脚本才看出真相的一半：
拐点（11-03）前后，分区分布几乎没变（生活记录 78%→73%），
但**话题分布剧烈变化**：

    居家好物   68% → 14%   （塌了）
    好物分享   98% → 45%   （塌了）
    家居生活    0% → 41%   （凭空冒出）
    卧室焕新   22% →  0%   （消失）

如果只看分区，会得到「内容没变」的错误结论。真正变的是**具体选题**：
从「毛绒四件套/暖被」这类垂直品类，稀释成「家居生活」这种泛主题词，
外加混入电竞房/修眉刀/穿搭等非家居内容。

设计原则
--------
1. **只看分布，不看绝对量**：漂移是「比例变了」，不是「发得多了」；
2. **同时看分区和话题**：只查一个会漏（见上）；
3. **用占比差而不是卡方检验**：样本量只有几十条，卡方不可靠；
   占比差（百分比点）直观且可解释；
4. **必须给出具体是哪个词变了**：只说「有漂移」没法行动。
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# 占比变化超过多少个百分点算「显著漂移」。
# 取 20：B 号真实数据里「居家好物 68%→14%」是 54 个点，
# 而正常的内容轮换一般不会让单个词变化超过 20 个点。
SIGNIFICANT_DELTA_PCT = 20.0

# 两侧各自至少多少条样本才做比较（太少时分母不稳）
MIN_SAMPLES_PER_SIDE = 5


@dataclass
class DistChange:
    """某个维度值的占比变化。"""
    name: str
    before_pct: float
    after_pct: float

    @property
    def delta(self) -> float:
        return self.after_pct - self.before_pct

    @property
    def significant(self) -> bool:
        return abs(self.delta) >= SIGNIFICANT_DELTA_PCT

    @property
    def disappeared(self) -> bool:
        """"以前有、现在没有了"。"""
        return self.before_pct >= SIGNIFICANT_DELTA_PCT and self.after_pct < 5

    @property
    def appeared(self) -> bool:
        """"以前没有、现在冒出来了"。"""
        return self.after_pct >= SIGNIFICANT_DELTA_PCT and self.before_pct < 5


@dataclass
class DriftReport:
    """漂移诊断结果。"""
    before_n: int = 0
    after_n: int = 0
    category_changes: list[DistChange] = field(default_factory=list)
    tag_changes: list[DistChange] = field(default_factory=list)

    @property
    def category_significant(self) -> list[DistChange]:
        return [c for c in self.category_changes if c.significant]

    @property
    def tag_significant(self) -> list[DistChange]:
        return [c for c in self.tag_changes if c.significant]

    @property
    def has_drift(self) -> bool:
        return bool(self.category_significant or self.tag_significant)

    @property
    def reliable(self) -> bool:
        return (self.before_n >= MIN_SAMPLES_PER_SIDE
                and self.after_n >= MIN_SAMPLES_PER_SIDE)


# ---------------------------------------------------------------------------
# 分布计算
# ---------------------------------------------------------------------------

def _distribution(works, key: str) -> Counter:
    """统计某个维度的出现次数。

    key="category" 时每条作品计一次；
    key="tags" 时每个话题计一次（一条作品可贡献多个话题）。
    """
    counter: Counter = Counter()
    for w in works:
        if key == "category":
            counter[w.category or "(未分区)"] += 1
        else:
            for tag in dict.fromkeys(w.tags):
                counter[tag] += 1
    return counter


def _to_pct(counter: Counter, total: int) -> dict[str, float]:
    """转成占比（0~100）。total 是作品条数，不是计数总和。

    注意：话题的计数总和会大于作品数（一条多标签），
    但如果用计数总和做分母，「一条作品 4 个标签」会让占比总和变成 400%，
    跨维度比较就失去意义。这里统一用**作品条数**做分母，
    于是「某话题出现在 X% 的作品里」，语义清晰且可直接比较。
    """
    if total <= 0:
        return {}
    return {k: v / total * 100 for k, v in counter.items()}


def compare_distributions(before, after, key: str) -> list[DistChange]:
    """比较两段作品的分布差异，按 |delta| 降序。"""
    cb = _distribution(before, key)
    ca = _distribution(after, key)
    pb = _to_pct(cb, len(before))
    pa = _to_pct(ca, len(after))

    names = set(pb) | set(pa)
    changes = [DistChange(name=n, before_pct=pb.get(n, 0.0),
                          after_pct=pa.get(n, 0.0)) for n in names]
    return sorted(changes, key=lambda c: abs(c.delta), reverse=True)


def detect_drift(before, after) -> DriftReport:
    """对比两段作品的内容分布，生成漂移报告。"""
    rep = DriftReport(before_n=len(before), after_n=len(after))
    if not before or not after:
        return rep
    rep.category_changes = compare_distributions(before, after, "category")
    rep.tag_changes = compare_distributions(before, after, "tags")
    return rep


def split_by_date(works, cut_date: str):
    """按日期把作品切两段（< cut_date 为 before，>= 为 after）。"""
    before = [w for w in works if (w.published_at or "")[:10] < cut_date]
    after = [w for w in works if (w.published_at or "")[:10] >= cut_date]
    return before, after


def auto_cut_date(works, recent_days: int = 7) -> str:
    """自动选拐点：把「最近 N 天」和「之前的」切开。

    为什么用最近 N 天而不是找最优切分：
    目的是回答「现在和以前一样吗」，不是找历史拐点
    （历史拐点由 report_play.detect_change_points 负责）。
    """
    from datetime import date, timedelta
    dates = sorted((w.published_at or "")[:10] for w in works
                   if (w.published_at or "")[:10])
    if not dates:
        return ""
    try:
        last = date.fromisoformat(dates[-1])
    except ValueError:
        return ""
    return (last - timedelta(days=recent_days)).isoformat()


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _fmt_row(c: DistChange, name_width: int = 14) -> str:
    """单行：名称 前% → 后%  Δ 标记。"""
    name = c.name[:name_width]
    arrow = "→"
    tag = ""
    if c.disappeared:
        tag = "  ❌ 消失"
    elif c.appeared:
        tag = "  🆕 新增"
    elif c.significant:
        tag = "  ⚠️ 变化大"
    return (f"  {name:<{name_width}} {c.before_pct:>5.0f}% {arrow} "
            f"{c.after_pct:>5.0f}%  Δ{c.delta:+5.0f}{tag}")


def render_drift(rep: DriftReport, cut_date: str = "",
                 top: int = 10) -> str:
    """渲染漂移报告。"""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(" 内容主题漂移检测")
    lines.append("=" * 78)

    if rep.before_n == 0 or rep.after_n == 0:
        lines.append("\n样本不足，无法比较（需要分组前后都有作品）")
        return "\n".join(lines)

    cut = f"（分界：{cut_date}）" if cut_date else ""
    lines.append(f"\n对比{cut}")
    lines.append(f"  前段 {rep.before_n} 条  vs  后段 {rep.after_n} 条")

    if not rep.reliable:
        lines.append(
            f"  ⚠️ 两侧样本均需 ≥{MIN_SAMPLES_PER_SIDE} 条，当前偏少，结论仅供参考")

    if not rep.has_drift:
        lines.append("\n✅ 未检测到显著主题漂移（内容主题保持稳定）")
        return "\n".join(lines)

    # 先报话题（更细，变化最明显），再报分区
    tag_sig = rep.tag_significant[:top]
    if tag_sig:
        lines.append("\n⚠️ 话题分布显著变化（占比按「出现在多少比例的作品里」计）：")
        for c in tag_sig:
            lines.append(_fmt_row(c))

        disappeared = [c.name for c in tag_sig if c.disappeared]
        appeared = [c.name for c in tag_sig if c.appeared]
        if disappeared:
            lines.append(f"\n  ❌ 已停用的核心话题：{'、'.join(disappeared)}")
            lines.append("     → 这些是之前的主力词，消失会直接影响搜索流量")
        if appeared:
            lines.append(f"  🆕 新出现的泛主题词：{'、'.join(appeared)}")
            lines.append("     → 泛词缺乏搜索心智（用户不会搜「家居生活」），"
                         "可能拉低分发")

    cat_sig = rep.category_significant[:top]
    if cat_sig:
        lines.append("\n⚠️ 分区分布显著变化：")
        for c in cat_sig:
            lines.append(_fmt_row(c))

    if not cat_sig and tag_sig:
        lines.append(
            "\n💡 分区没变但话题变了 —— 说明「大类没变，具体选题稀释了」。"
            "\n   只看分区会漏掉这种情况（2026-11-09 B 号排查时正是如此）。")

    lines.append("\n" + "=" * 78)
    return "\n".join(lines)

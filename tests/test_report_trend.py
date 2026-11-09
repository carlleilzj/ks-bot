"""逐日趋势与拐点检测测试。

这个功能的唯一目的是「防止归因错误」——所以测试重点不是数学正确，
而是**它不该误报**（误报会让人去改根本不用改的东西）和**不该漏报**
（漏报就会像 11-03 那次，把账号掉量读成话题问题）。
"""
from __future__ import annotations

import pytest

from bot import report_play as rp


def _w(play, day="2026-11-01", tags=None, cat="生活记录"):
    return rp.WorkRow(
        platform="kuaishou2", title="t", play=play,
        category=cat, tags=tags or [],
        published_at=f"{day}T10:00:00",
    )


def _mk_days(spec: list[tuple[str, list[int]]]) -> list[rp.DayStat]:
    return [rp.DayStat(date=d, plays=sorted(p)) for d, p in spec]


class TestDailyStats:
    def test_groups_by_date(self):
        works = [
            _w(100, "2026-11-01"), _w(300, "2026-11-01"),
            _w(50, "2026-11-02"),
        ]
        days = rp.daily_stats(works)
        assert len(days) == 2
        assert days[0].date == "2026-11-01"
        assert days[0].n == 2
        assert days[0].median == 200
        assert days[1].median == 50

    def test_skips_missing_play(self):
        """没有播放数据的作品不能算进当天（否则误显示成 0 播）。"""
        works = [_w(100, "2026-11-01"), _w(-1, "2026-11-01")]
        days = rp.daily_stats(works)
        assert days[0].n == 1

    def test_empty_day_not_created(self):
        works = [_w(-1, "2026-11-01")]
        assert rp.daily_stats(works) == []

    def test_sorted_ascending(self):
        works = [_w(100, "2026-11-03"), _w(100, "2026-11-01")]
        days = rp.daily_stats(works)
        assert [d.date for d in days] == ["2026-11-01", "2026-11-03"]


class TestDetectChangePoints:
    def test_detects_crash(self):
        """核心场景：11-03 起从 3000+ 掉到 100 左右。"""
        days = _mk_days([
            ("2026-10-24", [3150, 3153, 3180]),
            ("2026-10-25", [3173, 3130, 3191]),
            ("2026-10-26", [4084, 4539]),
            ("2026-10-28", [4205, 4100, 4130]),
            ("2026-10-29", [1690, 3567, 2098]),
            ("2026-11-03", [1287, 460, 382]),
            ("2026-11-04", [191, 86, 83]),
            ("2026-11-05", [371, 90, 59]),
            ("2026-11-06", [347, 66]),
            ("2026-11-08", [67, 24, 25]),
        ])
        points = rp.detect_change_points(days)
        assert points, "应当检测到拐点"
        top = points[0]
        assert top.ratio < 0.35, f"应判定为下跌，实际 ratio={top.ratio}"
        assert top.before_median > top.after_median

    def test_no_false_positive_on_stable(self):
        """稳定数据不能误报——误报会让人去改不该改的东西。"""
        days = _mk_days([
            ("2026-11-01", [3000, 3100, 2900]),
            ("2026-11-02", [3050, 2950, 3000]),
            ("2026-11-03", [3100, 3000, 2900]),
            ("2026-11-04", [2950, 3050, 3000]),
            ("2026-11-05", [3000, 3100, 2900]),
            ("2026-11-06", [3050, 2950]),
        ])
        assert rp.detect_change_points(days) == []

    def test_no_report_on_small_sample(self):
        """样本太少时宁可不报，避免用 2+2 条做主结论。"""
        days = _mk_days([
            ("2026-11-01", [3000, 3100]),
            ("2026-11-02", [100, 90]),
        ])
        assert rp.detect_change_points(days) == []

    def test_detects_rise(self):
        days = _mk_days([
            ("2026-11-01", [100, 110, 90, 105]),
            ("2026-11-02", [105, 95, 100, 110]),
            ("2026-11-03", [3000, 3100, 2900, 3050]),
            ("2026-11-04", [3050, 2950, 3000, 3100]),
        ])
        points = rp.detect_change_points(days)
        assert points and points[0].ratio > 1

    def test_median_resists_single_outlier(self):
        """一天里有一条 33 万爆款，不能把整体判成上涨。"""
        days = _mk_days([
            ("2026-11-01", [3000, 3100, 2900, 3050]),
            ("2026-11-02", [3050, 2950, 3000, 3100]),
            ("2026-11-03", [100, 90, 110, 95]),
            ("2026-11-04", [95, 105, 332493, 90]),   # 混入一条爆款
            ("2026-11-05", [90, 100, 110, 95]),
        ])
        points = rp.detect_change_points(days)
        assert points, "仍应识别为下跌"
        assert points[0].ratio < 1, "爆款不应把趋势翻成上涨"

    def test_merges_adjacent_points(self):
        """同一段下跌会被扫出多个拐点，应合并成一条。"""
        days = _mk_days([
            ("2026-11-01", [3000, 3100, 2900, 3050]),
            ("2026-11-02", [3050, 2950, 3000, 3100]),
            ("2026-11-03", [500, 480, 520, 490]),
            ("2026-11-04", [200, 190, 210, 205]),
            ("2026-11-05", [100, 90, 110, 95]),
            ("2026-11-06", [95, 105, 100, 90]),
        ])
        points = rp.detect_change_points(days)
        assert len(points) == 1, f"相邻拐点应合并，实际 {len(points)} 个"

    def test_confident_flag(self):
        """两侧样本都够（>=10 条）时标记为可信。"""
        # 12 天 × 4 条 = 48 条样本，拐点在第 7 天
        days = _mk_days([
            *[(f"2026-10-{d:02d}", [3000, 3100, 2900, 3050]) for d in range(10, 22)],
            *[(f"2026-11-{d:02d}", [100, 90, 110, 95]) for d in range(1, 8)],
        ])
        points = rp.detect_change_points(days)
        assert points, "应当检出拐点"
        # 合并后跨度为「10-10 起下跌」，前 48 条 / 后 28 条
        assert points[0].before_n >= 10
        assert points[0].after_n >= 10
        assert points[0].confident is True

    def test_not_confident_on_thin_sample(self):
        days = _mk_days([
            ("2026-11-01", [3000, 3100, 2900, 3050]),
            ("2026-11-02", [3050, 2950, 3000, 3100]),
            ("2026-11-03", [100, 90, 110, 95]),
            ("2026-11-04", [95, 105, 100, 90]),
        ])
        points = rp.detect_change_points(days)
        assert points and points[0].confident is False


class TestRenderTrend:
    def test_renders_bar_and_mark(self):
        days = _mk_days([("2026-11-01", [3000]), ("2026-11-02", [100])])
        points = [rp.ChangePoint(date="2026-11-02", before_median=3000,
                                 after_median=100, before_n=10, after_n=10,
                                 ratio=0.033, confident=True)]
        lines = rp.render_trend(days, points)
        text = "\n".join(lines)
        assert "2026-11-01" in text
        assert "拐点" in text
        assert "█" in text

    def test_empty_days(self):
        lines = rp.render_trend([], [])
        assert "无播放数据" in "\n".join(lines)


class TestRenderChangePoints:
    def test_no_points_message(self):
        text = "\n".join(rp.render_change_points([]))
        assert "未检测到显著拐点" in text

    def test_reports_direction_and_factor(self):
        p = rp.ChangePoint(date="2026-11-04", before_median=3150,
                           after_median=75, before_n=47, after_n=16,
                           ratio=0.024, confident=True)
        text = "\n".join(rp.render_change_points([p]))
        assert "下跌" in text
        assert "2026-11-04" in text
        assert "41.7" in text   # 1/0.024 ≈ 41.7

    def test_warns_against_topic_attribution(self):
        """必须明确提醒：拐点是时间信号，别归因到话题。"""
        p = rp.ChangePoint(date="2026-11-04", before_median=3000,
                           after_median=100, before_n=20, after_n=20,
                           ratio=0.033, confident=True)
        text = "\n".join(rp.render_change_points([p]))
        assert "不要归因到单个话题" in text

    def test_marks_thin_sample(self):
        p = rp.ChangePoint(date="2026-11-04", before_median=3000,
                           after_median=100, before_n=5, after_n=5,
                           ratio=0.033, confident=False)
        text = "\n".join(rp.render_change_points([p]))
        assert "样本偏少" in text


class TestReportIntegration:
    def test_trend_appears_before_groups(self):
        """趋势必须在分组之前——有拐点时分组结论是错的。"""
        works = [
            *[_w(3000, f"2026-10-{d:02d}") for d in range(10, 16)],
            *[_w(100, f"2026-10-{d:02d}") for d in range(16, 22)],
        ]
        text = rp.render_report(works, "kuaishou2")
        trend_pos = text.find("逐日趋势")
        cat_pos = text.find("【按分区】")
        assert trend_pos != -1 and cat_pos != -1
        assert trend_pos < cat_pos, "趋势必须排在分区统计之前"

    def test_warns_when_change_point_exists(self):
        works = [
            *[_w(3000, f"2026-10-{d:02d}") for d in range(10, 16)],
            *[_w(100, f"2026-10-{d:02d}") for d in range(16, 22)],
        ]
        text = rp.render_report(works, "kuaishou2")
        assert "由于存在拐点" in text
        assert "不能代表当前内容效果" in text

    def test_can_disable_trend(self):
        works = [_w(100)]
        text = rp.render_report(works, "kuaishou2", show_trend=False)
        assert "逐日趋势】（按发布日" not in text

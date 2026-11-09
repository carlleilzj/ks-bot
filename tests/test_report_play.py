"""播放效果报表测试。

报表算错比没有更糟——会导出错误结论去调整选题。
重点锁三个容易错的点：
1. 缺数据的作品必须计入 total_works（否则覆盖率虚高）；
2. 中位数不能被极值带偏（这是选它的唯一理由）；
3. 小样本必须被标记，不能让人拿 n=2 的结论去改策略。
"""
from __future__ import annotations

import json

import pytest

from bot import report_play as rp


def _w(platform="kuaishou", play=100, cat="搞笑", tags=None, title="t"):
    return rp.WorkRow(
        platform=platform, title=title, play=play,
        category=cat, tags=tags or [], published_at="2026-11-09T10:00:00",
    )


class TestPercentile:
    def test_single_value(self):
        assert rp._percentile([5], 0.5) == 5.0

    def test_two_values(self):
        assert rp._percentile([10, 20], 0.5) == 15.0

    def test_quartiles(self):
        vals = [1, 2, 3, 4, 5]
        assert rp._percentile(vals, 0.25) == pytest.approx(2.0)
        assert rp._percentile(vals, 0.75) == pytest.approx(4.0)

    def test_empty(self):
        assert rp._percentile([], 0.5) == 0.0


class TestGroupStat:
    def test_basic_stats(self):
        g = rp.GroupStat(name="x", plays=[100, 200, 300], total_works=3)
        assert g.n == 3
        assert g.median == 200
        assert g.mean == 200
        assert g.max_play == 300
        assert g.coverage == 1.0

    def test_median_resists_outlier(self):
        """核心：一条 33 万不能把中位数带飞（否则就会像均值一样误导）。"""
        normal = [100, 120, 110, 130, 140]
        with_outlier = normal + [331195]
        g = rp.GroupStat(name="x", plays=with_outlier, total_works=6)
        assert g.median < 200, f"中位数被极值带偏：{g.median}"
        assert g.mean > 50000, "均值应当被拉高（这正是不能看均值的原因）"

    def test_coverage_counts_missing(self):
        """缺数据的作品必须计入 total_works。"""
        g = rp.GroupStat(name="x", plays=[1, 2], total_works=10)
        assert g.n == 2
        assert g.coverage == 0.2

    def test_coverage_zero_when_no_works(self):
        assert rp.GroupStat(name="x").coverage == 0.0

    def test_zero_play_counted(self):
        g = rp.GroupStat(name="x", plays=[0, 0, 100], total_works=3)
        assert g.zero_play == 2

    def test_reliable_threshold(self):
        assert not rp.GroupStat(name="x", plays=[1, 2, 3, 4]).reliable
        assert rp.GroupStat(name="x", plays=[1, 2, 3, 4, 5]).reliable


class TestGroupByCategory:
    def test_groups_and_sorts_by_median(self):
        works = [
            _w(cat="搞笑", play=100),
            _w(cat="搞笑", play=120),
            _w(cat="情感", play=5000),
            _w(cat="情感", play=4000),
        ]
        stats = rp.group_by_category(works)
        assert [s.name for s in stats] == ["情感", "搞笑"]

    def test_missing_play_not_in_sample(self):
        works = [_w(cat="搞笑", play=100), _w(cat="搞笑", play=-1)]
        stats = rp.group_by_category(works)
        g = stats[0]
        assert g.n == 1
        assert g.total_works == 2
        assert g.coverage == 0.5

    def test_empty_category_named(self):
        works = [_w(cat="", play=50)]
        stats = rp.group_by_category(works)
        assert stats[0].name == "(未分区)"


class TestGroupByTag:
    def test_tag_appearing_once_filtered(self):
        works = [
            _w(tags=["热门", "独有1"], play=100),
            _w(tags=["热门", "独有2"], play=200),
        ]
        stats = rp.group_by_tag(works, min_works=2)
        names = [s.name for s in stats]
        assert "热门" in names
        assert "独有1" not in names, "只出现一次的话题不该进报表"

    def test_multi_tag_work_counted_once_per_tag(self):
        """一条作品有多个话题时，每个话题都算一条样本，但不能重复计同一条。"""
        works = [_w(tags=["a", "a", "b"], play=100)]
        stats = {s.name: s for s in rp.group_by_tag(works, min_works=1)}
        assert stats["a"].n == 1, "重复话题应去重"
        assert stats["b"].n == 1


class TestOverall:
    def test_totals(self):
        works = [_w(play=100), _w(play=300), _w(play=-1)]
        g = rp.overall(works)
        assert g.total_works == 3
        assert g.n == 2
        assert g.median == 200


class TestRender:
    def test_table_marks_unreliable(self):
        stats = [rp.GroupStat(name="tiny", plays=[1, 2], total_works=2)]
        lines = rp.render_table(stats, "测试")
        text = "\n".join(lines)
        assert "*" in text
        assert "样本" in text

    def test_report_warns_low_coverage(self):
        works = [_w(play=100), _w(play=-1), _w(play=-1), _w(play=-1)]
        text = rp.render_report(works, "kuaishou")
        assert "缺播放数据" in text
        assert "覆盖" in text

    def test_report_handles_no_data(self):
        works = [_w(play=-1)]
        text = rp.render_report(works, "kuaishou")
        assert "无有播放数据的样本" in text or "0 条有播放数据" in text

    def test_report_contains_reading_guide(self):
        works = [_w(play=100)]
        text = rp.render_report(works, "kuaishou")
        assert "中位" in text
        assert "均值" in text


class TestLoadWorks:
    def test_loads_and_parses_copy_json(self, tmp_path):
        from bot import db as db_mod

        d = db_mod.Database(tmp_path / "t.db")
        # 造一条 task + job
        meta = _fake_meta()
        tid = d.insert_video(meta)
        copy = {"kuaishou": {"category": "搞笑", "tags": ["搞笑动画", "萌宠"]}}
        d.update(tid, title="标题", copy_json=json.dumps(copy, ensure_ascii=False),
                 play_count=500)
        d.create_publish_jobs(tid, ["kuaishou"])
        with d._lock:
            d.conn.execute("UPDATE publish_jobs SET state='PUBLISHED', "
                           "published_at='2026-11-09T10:00:00'")
            d.conn.commit()

        works = rp.load_works(d, "kuaishou")
        assert len(works) == 1
        w = works[0]
        assert w.play == 500
        assert w.category == "搞笑"
        assert w.tags == ["搞笑动画", "萌宠"]

    def test_null_play_becomes_minus_one(self, tmp_path):
        from bot import db as db_mod

        d = db_mod.Database(tmp_path / "t.db")
        tid = d.insert_video(_fake_meta())
        d.update(tid, title="标题", copy_json="{}")
        d.create_publish_jobs(tid, ["kuaishou"])
        with d._lock:
            d.conn.execute("UPDATE publish_jobs SET state='PUBLISHED'")
            d.conn.commit()

        works = rp.load_works(d, "kuaishou")
        assert works[0].play == -1


def _fake_meta():
    """构造一条最小可用的 VideoMeta。"""
    from bot.source.downloader import VideoMeta
    return VideoMeta(
        source_url="https://example.com/v/1",
        platform="kuaishou",
        video_id="vid1",
        shortcode="ks_vid1",
        username="tester",
        title="t",
        caption="",
        thumbnail_url="",
        duration=10.0,
        permalink="",
    )

"""内容主题漂移检测测试。

核心价值：能看出「大类没变但具体选题稀释了」。
2026-11-09 B 号排查时，分区分布几乎没变（生活记录 78%→73%），
但话题分布剧烈变化——只看分区会得出「内容没变」的错误结论。

所以测试重点：
1. 话题要能单独暴露漂移（哪怕分区不变）
2. 「消失」与「新增」要被明确标出，不能只报个 delta
3. 占比用作品数做分母，不能让多标签把分母撑成 400%
"""
from __future__ import annotations

import pytest

from bot import report_drift as rd


def _w(day, tags=None, cat="生活记录"):
    return type("W", (), {
        "published_at": f"{day}T10:00:00",
        "category": cat,
        "tags": tags or [],
        "play": 100,
        "platform": "kuaishou2",
        "title": "t",
    })()


class TestDistribution:
    def test_category_counts_once_per_work(self):
        works = [_w("2026-11-01", cat="搞笑"), _w("2026-11-01", cat="搞笑")]
        c = rd._distribution(works, "category")
        assert c["搞笑"] == 2

    def test_tags_count_each_tag(self):
        works = [_w("2026-11-01", tags=["a", "b"]), _w("2026-11-01", tags=["a"])]
        c = rd._distribution(works, "tags")
        assert c["a"] == 2
        assert c["b"] == 1

    def test_duplicate_tag_in_one_work_counted_once(self):
        works = [_w("2026-11-01", tags=["a", "a", "b"])]
        c = rd._distribution(works, "tags")
        assert c["a"] == 1

    def test_pct_uses_work_count_as_denominator(self):
        """一条作品 4 个标签时，占比总和不能变成 400%。

        用作品数做分母 → 「该话题出现在 100% 的作品里」，
        语义清晰且跨维度可比。
        """
        works = [_w("2026-11-01", tags=["a", "b", "c", "d"])]
        pct = rd._to_pct(rd._distribution(works, "tags"), len(works))
        assert all(v == 100.0 for v in pct.values())
        assert sum(pct.values()) == 400.0   # 4 个标签各占 100%

    def test_empty_works(self):
        assert rd._to_pct(rd._distribution([], "tags"), 0) == {}


class TestDistChange:
    def test_delta_sign(self):
        c = rd.DistChange(name="x", before_pct=60, after_pct=10)
        assert c.delta == -50

    def test_significant_threshold(self):
        assert rd.DistChange("x", 60, 30).significant       # -30
        assert not rd.DistChange("x", 60, 50).significant   # -10

    def test_disappeared(self):
        c = rd.DistChange("居家好物", before_pct=68, after_pct=2)
        assert c.disappeared is True

    def test_not_disappeared_when_was_rare(self):
        """以前只占 5%，现在没了，不算「消失」（本来就不主力）。"""
        c = rd.DistChange("x", before_pct=5, after_pct=0)
        assert c.disappeared is False

    def test_appeared(self):
        c = rd.DistChange("家居生活", before_pct=0, after_pct=41)
        assert c.appeared is True


class TestCompareDistributions:
    def test_sorted_by_abs_delta(self):
        before = [_w("2026-11-01", tags=["a"]), _w("2026-11-01", tags=["a"]),
                  _w("2026-11-01", tags=["b"])]
        after = [_w("2026-11-02", tags=["c"]), _w("2026-11-02", tags=["c"]),
                 _w("2026-11-02", tags=["a"])]
        changes = rd.compare_distributions(before, after, "tags")
        # 分母是作品数(3)，不是标签总数：
        #   a: 67%→33% (Δ-33)、b: 33%→0% (Δ-33)、c: 0%→67% (Δ+67)
        assert changes[0].name == "c"
        assert changes[0].delta == pytest.approx(67, abs=1)
        assert {c.name for c in changes[1:]} == {"a", "b"}


class TestDetectDrift:
    def test_detects_b_account_real_pattern(self):
        """复现 B 号真实漂移：话题塌了，分区没动。"""
        before = [
            _w("2026-10-28", tags=["居家好物", "卧室焕新"]),
            _w("2026-10-28", tags=["居家好物", "好物分享"]),
            _w("2026-10-29", tags=["居家好物", "卧室改造"]),
            _w("2026-10-30", tags=["居家好物", "好物分享"]),
            _w("2026-10-31", tags=["居家好物", "四件套"]),
        ]
        after = [
            _w("2026-11-04", tags=["家居生活", "梦中情房"]),
            _w("2026-11-05", tags=["家居生活", "梦中情房"]),
            _w("2026-11-06", tags=["家居生活"]),
            _w("2026-11-08", tags=["家居生活"]),
            _w("2026-11-09", tags=["家居生活"]),
        ]
        rep = rd.detect_drift(before, after)

        assert rep.has_drift

        # 分区没变（都是生活记录）
        assert not rep.category_significant, "分区不该被判定有变化"

        # 话题必须报出来
        names = {c.name: c for c in rep.tag_significant}
        assert "居家好物" in names and names["居家好物"].disappeared
        assert "家居生活" in names and names["家居生活"].appeared

    def test_no_drift_on_stable_content(self):
        before = [_w("2026-11-01", tags=["居家好物"]),
                  _w("2026-11-01", tags=["居家好物"])]
        after = [_w("2026-11-05", tags=["居家好物"]),
                 _w("2026-11-05", tags=["居家好物"])]
        rep = rd.detect_drift(before, after)
        assert not rep.has_drift

    def test_empty_side(self):
        rep = rd.detect_drift([], [_w("2026-11-01")])
        assert rep.has_drift is False
        assert rep.before_n == 0

    def test_reliability_flag(self):
        rep = rd.detect_drift([_w("2026-11-01")], [_w("2026-11-05")])
        assert rep.reliable is False
        rep2 = rd.detect_drift(
            [_w("2026-11-01")] * 6, [_w("2026-11-05")] * 6)
        assert rep2.reliable is True


class TestSplitAndCut:
    def test_split_by_date(self):
        works = [_w("2026-11-01"), _w("2026-11-03"), _w("2026-11-05")]
        b, a = rd.split_by_date(works, "2026-11-03")
        assert len(b) == 1
        assert len(a) == 2
        assert b[0].published_at.startswith("2026-11-01")

    def test_auto_cut_date(self):
        works = [_w("2026-11-01"), _w("2026-11-09")]
        cut = rd.auto_cut_date(works, recent_days=7)
        assert cut == "2026-11-02"

    def test_auto_cut_empty(self):
        assert rd.auto_cut_date([]) == ""


class TestRender:
    def test_reports_disappeared_and_appeared(self):
        rep = rd.DriftReport(
            before_n=10, after_n=10,
            tag_changes=[
                rd.DistChange("居家好物", 68, 2),
                rd.DistChange("家居生活", 0, 41),
            ])
        text = rd.render_drift(rep, cut_date="2026-11-03")
        assert "消失" in text
        assert "新增" in text
        assert "居家好物" in text
        assert "家居生活" in text

    def test_explains_category_blind_spot(self):
        """必须点明「分区没变但话题变了」这种情况。"""
        rep = rd.DriftReport(
            before_n=10, after_n=10,
            tag_changes=[rd.DistChange("居家好物", 68, 2)])
        text = rd.render_drift(rep)
        assert "只看分区会漏" in text

    def test_no_drift_message(self):
        rep = rd.DriftReport(before_n=10, after_n=10)
        text = rd.render_drift(rep)
        assert "未检测到显著主题漂移" in text

    def test_warns_on_thin_sample(self):
        rep = rd.DriftReport(before_n=2, after_n=2,
                             tag_changes=[rd.DistChange("居家好物", 100, 0)])
        text = rd.render_drift(rep)
        assert "样本" in text and "参考" in text

    def test_empty_when_no_data(self):
        text = rd.render_drift(rd.DriftReport(before_n=0, after_n=0))
        assert "样本不足" in text

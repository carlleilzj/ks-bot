"""播放量测量偏差的回归测试。

2026-11-09 排查 A/B 号播放暴跌时发现的真实问题
------------------------------------------------
tasks.play_count 只存「最后一次测量值」，而各条作品的最后测量时间不同：

    10-30 发布的作品  测于 237h 后  中位 382
    11-09 发布的作品  测于   6h 后  中位  20

把这两组放在同一张按日期排序的表里，就得到一条「暴跌 19 倍」的曲线。
那不是账号表现，是长尾曲线上的不同位置。

按测量年龄对齐后，B 号 11-03 拐点的真实幅度从 22 倍降到 5.4 倍。

所以本文件测的不是「播放量对不对」，而是
**报表会不会把测量节奏误报成账号衰退**。
"""
from __future__ import annotations

import sqlite3

import pytest

from bot import report_play as rp


def _w(play, age=None, day="2026-11-01", tags=None, cat="生活记录"):
    return rp.WorkRow(
        platform="kuaishou2", title="t", play=play,
        category=cat, tags=tags or [],
        published_at=f"{day}T10:00:00", age_hours=age,
    )


def _db(tmp_path):
    from bot.db import Database
    return Database(path=tmp_path / "t.db")


# ---------- 年龄计算 ----------

class TestAgeHours:
    def test_basic(self):
        age = rp._age_hours("2026-11-01T10:00:00", "2026-11-04T10:00:00")
        assert age == pytest.approx(72.0)

    def test_missing_either_returns_none(self):
        assert rp._age_hours(None, "2026-11-04T10:00:00") is None
        assert rp._age_hours("2026-11-01T10:00:00", None) is None
        assert rp._age_hours("", "") is None

    def test_negative_clamped_to_none(self):
        """stats_at 早于 published_at 是数据错误，不能当成负年龄参与统计。"""
        assert rp._age_hours("2026-11-04T10:00:00", "2026-11-01T10:00:00") is None

    def test_unparseable(self):
        assert rp._age_hours("昨天", "2026-11-04T10:00:00") is None


# ---------- 年龄跨度告警 ----------

class TestAgeSpread:
    def test_detects_mixed_ages(self):
        """6h 与 237h 混在一起必须告警。"""
        works = [_w(20, age=6), _w(382, age=237)]
        lo, hi, warn = rp.age_spread(works)
        assert lo == 6 and hi == 237
        assert warn is True

    def test_uniform_ages_no_warning(self):
        works = [_w(100, age=70), _w(200, age=75)]
        _, _, warn = rp.age_spread(works)
        assert warn is False

    def test_no_data(self):
        assert rp.age_spread([_w(-1, age=None)]) == (0.0, 0.0, False)

    def test_small_absolute_gap_not_flagged(self):
        """6h vs 12h 比值 2 但绝对差只有 6 小时，不算问题 —— 都在预热期。"""
        works = [_w(20, age=6), _w(30, age=12)]
        _, _, warn = rp.age_spread(works)
        assert warn is False


# ---------- 年龄分桶 ----------

class TestAgeBreakdown:
    def test_buckets(self):
        works = [_w(10, age=3), _w(20, age=8), _w(2000, age=50),
                 _w(3000, age=100)]
        rows = rp.age_breakdown(works)
        labels = {r[0]: (r[1], r[2]) for r in rows}
        assert labels["0~12h"] == (2, 15.0)
        assert labels["36~72h"] == (1, 2000.0)
        assert labels["72~120h"] == (1, 3000.0)

    def test_skips_works_without_data(self):
        rows = rp.age_breakdown([_w(-1, age=50), _w(100, age=None)])
        assert rows == []


# ---------- 分层对比（剥离测量偏差的正解）----------

class TestStratifiedCompare:
    def test_compares_within_same_age_bucket(self):
        """只在同一年龄层内比较 —— 这是剥离测量偏差的关键。"""
        works = [
            # 120~180h 层：旧 2000、新 500（真实下滑）
            _w(2000, age=150, day="2026-10-28"),
            _w(500, age=150, day="2026-11-05"),
            # 12~36h 层：都只有 200（这一层没有下滑）
            _w(200, age=20, day="2026-10-28"),
            _w(200, age=20, day="2026-11-05"),
        ]
        rows = rp.stratified_compare(works, "2026-11-03")
        by = {r.age_label: r for r in rows}
        assert by["120~180h"].ratio == pytest.approx(0.25)
        assert by["12~36h"].ratio == pytest.approx(1.0)

    def test_does_not_mix_ages(self):
        """老样本的年龄不应污染新样本的对比。

        若不分层，2000（150h）与 20（5h）会被直接比成 100 倍暴跌。
        分层后 5h 的样本根本不进 120~180h 层。
        """
        works = [
            _w(2000, age=150, day="2026-10-28"),
            _w(20, age=5, day="2026-11-08"),
        ]
        rows = rp.stratified_compare(works, "2026-11-03")
        assert rows == [], "两侧年龄差太大，不应产生任何对比层"

    def test_skips_warmup_bucket(self):
        """12~36h 以下的预热期样本不参与（波动太大）。"""
        works = [_w(100, age=8, day="2026-10-28"),
                 _w(100, age=8, day="2026-11-05")]
        assert rp.stratified_compare(works, "2026-11-03", min_age_hours=12) == []

    def test_requires_both_sides(self):
        works = [_w(100, age=150, day="2026-10-28"),
                 _w(100, age=150, day="2026-10-29")]
        assert rp.stratified_compare(works, "2026-11-03") == []

    def test_render_flags_consistent_decline(self):
        rows = [
            rp.StratumCompare("72~120h", 72, 120, 4854, 10, 210, 6),
            rp.StratumCompare("120~180h", 120, 180, 660, 15, 226, 11),
        ]
        text = "\n".join(rp.render_stratified(rows, "2026-10-29"))
        assert "一致下跌" in text
        assert "排除测量偏差后" in text

    def test_render_flags_flat(self):
        rows = [rp.StratumCompare("120~180h", 120, 180, 800, 8, 790, 7)]
        text = "\n".join(rp.render_stratified(rows, "2026-11-03"))
        assert "基本持平" in text
        assert "测量偏差" in text

    def test_render_flags_mixed_direction(self):
        rows = [
            rp.StratumCompare("72~120h", 72, 120, 1000, 5, 100, 5),
            rp.StratumCompare("120~180h", 120, 180, 100, 5, 1000, 5),
        ]
        text = "\n".join(rp.render_stratified(rows, "2026-11-03"))
        assert "方向不一致" in text

    def test_render_empty(self):
        text = "\n".join(rp.render_stratified([], "2026-11-03"))
        assert "无法分层对比" in text


# ---------- 话题必须分测量年龄层比较（2026-11-09 真实事故）----------

class TestTagMustBeStratified:
    """跨年龄层比话题 = 拿「测量时机」当「话题效果」。

    真实事故：B 号「家居生活」整体中位 86、「居家好物」2683，看起来差
    31 倍，据此把「家居生活」列为禁用词并部署。同年龄层内实际是 78 vs 83。
    原因是两者完美共线——「家居生活」只出现在账号低谷期（测量年龄短）。

    这条测试钉住：报表必须提供分层视图，且分层后能看出「不是话题问题」。
    """

    def test_cross_age_comparison_is_misleading(self):
        """构造事故数据：同一话题在两层表现相同，但整体看起来极差。"""
        works = [
            # 老层（250h）：两个话题都是 3000
            _w(3000, age=250, tags=["居家好物"], day="2026-10-25"),
            _w(3000, age=250, tags=["居家好物"], day="2026-10-26"),
            _w(3000, age=250, tags=["居家好物"], day="2026-10-27"),
            # 新层（80h）：两个话题都是 80（3 条才够一层）
            _w(80, age=80, tags=["家居生活"], day="2026-11-05"),
            _w(80, age=80, tags=["居家好物"], day="2026-11-05"),
            _w(80, age=80, tags=["家居生活", "居家好物"], day="2026-11-06"),
        ]
        # 整体看：家居生活 80 vs 居家好物 1540 —— 看起来差 19 倍
        rows = rp.group_by_tag(works)
        by = {g.name: g for g in rows}
        assert by["家居生活"].median == 80
        assert by["居家好物"].median > 1000

        # 分层看：同一层内「家居生活 80 vs 居家好物 80」——没有差别
        strata = dict(rp.group_by_tag_by_age(works))
        assert "72~120h" in strata
        low = {t: m for t, n, m in strata["72~120h"]}
        assert low["家居生活"] == low["居家好物"] == 80

    def test_report_contains_stratified_table(self):
        works = [
            _w(3000, age=250, tags=["居家好物"], day="2026-10-25"),
            _w(3000, age=250, tags=["居家好物"], day="2026-10-26"),
            _w(3000, age=250, tags=["居家好物"], day="2026-10-27"),
            _w(80, age=80, tags=["家居生活"], day="2026-11-05"),
            _w(80, age=80, tags=["居家好物"], day="2026-11-05"),
            _w(80, age=80, tags=["家居生活", "居家好物"], day="2026-11-06"),
        ]
        text = rp.render_report(works, "kuaishou2")
        assert "按话题 × 测量年龄" in text
        assert "同一层内比较才是比较话题" in text
        assert "所有层" in text, "必须说明判据：所有层都落后才算话题差"

    def test_warns_against_cross_age_tag_comparison(self):
        works = [_w(100, age=80, tags=["x"])]
        text = rp.render_report(works, "kuaishou2")
        assert "不能跨测量年龄比较" in text

    def test_stratified_table_skips_thin_layers(self):
        """某层样本 <3 条时不输出（中位数不稳）。"""
        works = [_w(100, age=80, tags=["a"]), _w(200, age=80, tags=["a"])]
        assert rp.group_by_tag_by_age(works) == []

    def test_stratified_table_requires_min_works_per_tag(self):
        """话题在该层只出现 1 次时不列出。"""
        works = [
            _w(100, age=80, tags=["rare"]),
            _w(100, age=80, tags=["common"]),
            _w(100, age=80, tags=["common"]),
        ]
        strata = dict(rp.group_by_tag_by_age(works))
        tags = {t for t, _, _ in strata["72~120h"]}
        assert "common" in tags
        assert "rare" not in tags

    def test_render_empty_when_no_layers(self):
        text = "\n".join(rp.render_tag_by_age([]))
        assert "无法分层看话题" in text


# ---------- 报表必须点出这个问题 ----------

class TestReportWarnsOnAgeBias:
    def test_report_flags_uncomparable_trend(self):
        works = [_w(20, age=6, day="2026-11-09"),
                 _w(382, age=237, day="2026-10-30")]
        text = rp.render_report(works, "kuaishou2")
        assert "测量年龄不一致" in text
        assert "不可直接比较" in text
        assert "--mature-only" in text

    def test_report_shows_age_table(self):
        works = [_w(20, age=6), _w(2000, age=50)]
        text = rp.render_report(works, "kuaishou2")
        assert "按测量年龄" in text
        assert "0~12h" in text

    def test_no_warning_when_ages_uniform(self):
        works = [_w(100, age=70), _w(200, age=74)]
        text = rp.render_report(works, "kuaishou2")
        assert "测量年龄不一致 —— 下面的日趋势" not in text

    def test_mature_only_announces_filter(self):
        works = [_w(100, age=80)]
        text = rp.render_report(works, "kuaishou2", mature_only=True)
        assert "成熟度过滤" in text

    def test_extra_note_rendered(self):
        works = [_w(100, age=80)]
        text = rp.render_report(works, "kuaishou2", mature_only=True,
                                extra_note="（命中 5 条）")
        assert "命中 5 条" in text


# ---------- 快照落库 ----------

class TestPlaySnapshots:
    def test_snapshot_saved_and_idempotent(self, tmp_path):
        db = _db(tmp_path)
        assert db.add_play_snapshot(1, "kuaishou2", 100, 5,
                                    "2026-11-04T10:00:00", 72.0) is True
        # 同一 (task, platform, measured_at) 重复写不产生第二条
        assert db.add_play_snapshot(1, "kuaishou2", 100, 5,
                                    "2026-11-04T10:00:00", 72.0) is False
        rows = db.play_snapshots(platform="kuaishou2")
        assert len(rows) == 1
        assert rows[0]["play_count"] == 100
        assert rows[0]["age_hours"] == 72.0

    def test_keeps_history_across_days(self, tmp_path):
        """同一条作品多次测量都要保留 —— 这是对齐与看曲线的唯一依据。"""
        db = _db(tmp_path)
        db.add_play_snapshot(1, "kuaishou2", 50, None, "2026-11-02T10:00:00", 24.0)
        db.add_play_snapshot(1, "kuaishou2", 900, None, "2026-11-04T10:00:00", 72.0)
        db.add_play_snapshot(1, "kuaishou2", 1500, None, "2026-11-09T10:00:00", 192.0)
        rows = db.play_snapshots(task_id=1)
        assert [r["play_count"] for r in rows] == [50, 900, 1500]

    def test_filter_by_platform(self, tmp_path):
        db = _db(tmp_path)
        db.add_play_snapshot(1, "kuaishou", 10, None, "2026-11-04T10:00:00", 72.0)
        db.add_play_snapshot(1, "kuaishou2", 20, None, "2026-11-04T10:00:00", 72.0)
        assert len(db.play_snapshots(platform="kuaishou")) == 1
        assert len(db.play_snapshots()) == 2


# ---------- persist 必须落快照 ----------

class TestPersistWritesSnapshots:
    def _seed_task(self, db, published_at="2026-11-01T10:00:00"):
        # state 必须是 NOTIFIED/READY —— persist 只在这两个状态下找候选
        # （生产库里已发布任务的实际状态是 NOTIFIED，见 db.py State）
        from bot.db import now_iso
        ts = now_iso()
        with db._lock:
            cur = db.conn.execute(
                """INSERT INTO tasks (ig_media_id, shortcode, state, published_at,
                                      created_at, updated_at, tags)
                   VALUES (?,?,?,?,?,?,?)""",
                ("m1", "sc1", "NOTIFIED", published_at, ts, ts, '["居家好物"]'),
            )
            tid = cur.lastrowid
            db.conn.execute(
                """INSERT INTO publish_jobs
                   (task_id, platform, state, published_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (tid, "kuaishou2", "PUBLISHED", published_at, ts, ts),
            )
            db.conn.commit()
        return tid

    def test_persist_creates_snapshot_with_age(self, tmp_path):
        from datetime import datetime, timedelta

        from bot.monitor import stats as sm
        db = _db(tmp_path)
        pub = "2026-11-01T10:00:00"
        tid = self._seed_task(db, pub)
        st = sm.WorkStat(published_at="2026-11-01 10:00", play_count=888,
                         tags=["居家好物"])
        res = sm.persist(db, [st], platform="kuaishou2")
        assert res["updated"] == 1
        assert res["snapshots"] == 1

        snaps = db.play_snapshots(task_id=tid)
        assert len(snaps) == 1
        assert snaps[0]["play_count"] == 888
        # age_hours 应约等于「现在 - 11-01 10:00」
        expect = (datetime.now() - datetime(2026, 11, 1, 10, 0)).total_seconds() / 3600
        assert snaps[0]["age_hours"] == pytest.approx(expect, abs=1)

    def test_persist_twice_same_second_no_duplicate(self, tmp_path):
        """两次抓取落在同一秒时不重复写快照（幂等）。"""
        from bot.monitor import stats as sm
        db = _db(tmp_path)
        self._seed_task(db)
        st = sm.WorkStat(published_at="2026-11-01 10:00", play_count=1, tags=[])
        sm.persist(db, [st], platform="kuaishou2")
        # 同秒第二次：tasks 仍会更新，但快照唯一索引拦住
        res2 = sm.persist(db, [st], platform="kuaishou2")
        assert res2["snapshots"] == 0


# ---------- 按年龄对齐 ----------

class TestAlignByAge:
    def _seed(self, db, tid, platform, published_at, snaps):
        from bot.db import now_iso
        ts = now_iso()
        with db._lock:
            db.conn.execute(
                """INSERT INTO tasks (id, ig_media_id, shortcode, state,
                                      published_at, created_at, updated_at, copy_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (tid, f"m{tid}", f"sc{tid}", "NOTIFIED", published_at, ts, ts,
                 '{"kuaishou2": {"category": "生活记录", "tags": ["居家好物"]}}'),
            )
            db.conn.execute(
                """INSERT INTO publish_jobs
                   (task_id, platform, state, published_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (tid, platform, "PUBLISHED", published_at, ts, ts),
            )
            db.conn.commit()
        for play, age, at in snaps:
            db.add_play_snapshot(tid, platform, play, None, at, age)

    def test_picks_snapshot_closest_to_target(self, tmp_path):
        db = _db(tmp_path)
        self._seed(db, 1, "kuaishou2", "2026-11-01T10:00:00", [
            (50, 6.0, "2026-11-01T16:00:00"),
            (900, 72.0, "2026-11-04T10:00:00"),
            (1500, 192.0, "2026-11-09T10:00:00"),
        ])
        works, note = rp.align_by_age(db, "kuaishou2")
        assert len(works) == 1
        assert works[0].play == 900, "应取最接近 72h 的那次，而不是最新值"
        assert works[0].age_hours == 72.0
        assert "按年龄对齐" in note

    def test_ignores_snapshots_outside_tolerance(self, tmp_path):
        db = _db(tmp_path)
        self._seed(db, 1, "kuaishou2", "2026-11-01T10:00:00", [
            (50, 6.0, "2026-11-01T16:00:00"),
            (1500, 300.0, "2026-11-14T10:00:00"),
        ])
        works, note = rp.align_by_age(db, "kuaishou2")
        assert works == []
        assert "无法对齐" in note

    def test_empty_history_explains_why(self, tmp_path):
        db = _db(tmp_path)
        works, note = rp.align_by_age(db, "kuaishou2")
        assert works == []
        assert "无播放快照历史" in note

    def test_alignment_removes_false_collapse(self, tmp_path):
        """核心回归：不对齐看起来暴跌，对齐后基本持平。

        造两组作品：
          旧组 —— 只有 300h 的快照（播放 2000）
          新组 —— 只有 24h 的快照（播放 100）
        不控年龄时「中位从 2000 掉到 100」= 20 倍暴跌。
        但对齐到 72h 后，两组各有自己的 72h 快照（800 vs 700），
        真实差距几乎为零。
        """
        db = _db(tmp_path)
        for i in range(1, 5):     # 旧组 4 条
            self._seed(db, i, "kuaishou2", f"2026-10-{20+i:02d}T10:00:00", [
                (2000, 300.0, "2026-11-03T10:00:00"),
                (800, 72.0, f"2026-10-{23+i:02d}T10:00:00"),
            ])
        for i in range(5, 9):     # 新组 4 条
            self._seed(db, i, "kuaishou2", f"2026-11-{i-4:02d}T10:00:00", [
                (100, 24.0, f"2026-11-{i-3:02d}T10:00:00"),
                (700, 72.0, "2026-11-08T10:00:00"),
            ])

        # 不对齐（用 tasks 里的「最新值」），这里手工模拟
        naive_old, naive_new = 2000, 100
        naive_ratio = naive_old / naive_new
        assert naive_ratio >= 15, "不控年龄时确实会看到 20 倍级暴跌"

        works, _ = rp.align_by_age(db, "kuaishou2")
        assert len(works) == 8
        old = [w.play for w in works if w.published_at < "2026-10-25"]
        new = [w.play for w in works if w.published_at >= "2026-11-01"]
        assert min(old) == 800 and min(new) == 700
        assert abs(max(old) / max(new) - 1) < 0.2, "对齐后应基本持平"

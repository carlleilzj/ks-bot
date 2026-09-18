"""播放量回流 的回归测试。

背景（2026-09-19）：tasks 表原本没有播放量字段，AI 生成话题只能靠
先验审美，导致话题退化。本模块补上"抓取 → 落库 → 供话题优化"的回路。
"""
from __future__ import annotations

import pytest

from bot.monitor import stats as sm


class TestParseCardText:
    def test_full_card(self):
        """完整卡片：日期 + 播放 + 点赞 + 话题。"""
        text = ("【纯享放松】安静看完太解压了 编辑作品 设置权限 作品置顶 删除作品 "
                "2026年09月18日 10:03 已发布 播放488 点赞33 评论4 分享0 收藏2 "
                "完播率17.05% #无声视频 #治愈 #纯享放松 #解压")
        st = sm.parse_card_text(text)
        assert st is not None
        assert st.play_count == 488
        assert st.like_count == 33
        assert st.published_at == "2026-09-18 10:03"
        assert st.tags == ["无声视频", "治愈", "纯享放松", "解压"]

    def test_single_digit_month_day_zero_padded(self):
        text = "播放12 #测试 2026年9月8日 09:05"
        st = sm.parse_card_text(text)
        assert st.published_at == "2026-09-08 09:05"

    def test_play_with_space(self):
        """平台可能输出「播放 488」带空格。"""
        st = sm.parse_card_text("播放 488 2026年09月18日 10:03")
        assert st.play_count == 488

    def test_returns_none_without_play(self):
        assert sm.parse_card_text("没有播放数据") is None
        assert sm.parse_card_text("") is None

    def test_like_optional(self):
        st = sm.parse_card_text("播放100 #X 2026年09月18日 10:03")
        assert st.play_count == 100
        assert st.like_count is None

    def test_tags_strip_trailing_ui_text(self):
        """回归：卡片文本是连续拼接的，话题尾巴会粘连 UI 文案。

        实测脏数据长这样：
            #解压编辑作品设置权限作品置顶删除作品2026年09月18日
        必须剥成干净的「解压」，否则话题统计全被污染。
        """
        text = ("标题 编辑作品 设置权限 作品置顶 删除作品 "
                "2026年09月18日 10:03 已发布 播放488 点赞33 "
                "#无声视频 #治愈 #纯享放松 #解压"
                "编辑作品设置权限作品置顶删除作品2026年09月18日")
        st = sm.parse_card_text(text)
        assert st.tags == ["无声视频", "治愈", "纯享放松", "解压"], \
            f"话题应剥离 UI 尾巴，实际 {st.tags}"

    def test_tags_without_spaces_between(self):
        """话题之间没有空格时也要正确切分（实测平台会这样输出）。"""
        text = ("播放1080 2026年09月16日 12:08 "
                "#无声高能#细节控必看#神反转编辑作品设置权限")
        st = sm.parse_card_text(text)
        assert st.tags == ["无声高能", "细节控必看", "神反转"], st.tags

    def test_overlong_tag_rejected(self):
        """超过 12 字的"标签"是切分失败的产物，应丢弃。"""
        text = "播放100 2026年09月18日 10:03 #这是一个非常非常长的不合法标签名字啊啊啊啊"
        st = sm.parse_card_text(text)
        assert all(len(t) <= 12 for t in st.tags), st.tags

    def test_strips_whitespace_and_newlines(self):
        st = sm.parse_card_text("播放\n  488\n  #测试\n2026年09月18日 10:03")
        assert st.play_count == 488


class TestDedupe:
    def test_removes_identical_cards(self):
        """同作品在嵌套 DOM 里会出现两次，必须去重。"""
        s1 = sm.WorkStat(published_at="2026-09-18 10:03", play_count=488,
                         tags=["无声视频"])
        s2 = sm.WorkStat(published_at="2026-09-18 10:03", play_count=488,
                         tags=["无声视频"])
        out = sm._dedupe([s1, s2])
        assert len(out) == 1

    def test_keeps_distinct(self):
        s1 = sm.WorkStat(published_at="2026-09-18 10:03", play_count=488)
        s2 = sm.WorkStat(published_at="2026-09-18 12:08", play_count=67)
        assert len(sm._dedupe([s1, s2])) == 2


class TestTopPerformingTags:
    def test_filters_by_min_play(self):
        stats = [
            sm.WorkStat(play_count=1500, tags=["无声视频", "细节控必看"]),
            sm.WorkStat(play_count=100, tags=["无声视频", "解压"]),
        ]
        rows = sm.top_performing_tags(stats, min_play=500)
        tags = [r[0] for r in rows]
        assert "细节控必看" in tags
        assert "解压" not in tags, "低播放的话题不该进榜"

    def test_sorted_by_max_play(self):
        stats = [
            sm.WorkStat(play_count=1000, tags=["A"]),
            sm.WorkStat(play_count=1500, tags=["B"]),
        ]
        rows = sm.top_performing_tags(stats, min_play=100)
        assert rows[0][0] == "B"


class TestSummarize:
    def test_basic(self):
        stats = [sm.WorkStat(play_count=p) for p in (100, 500, 1500)]
        s = sm.summarize(stats)
        assert s["count"] == 3
        assert s["max"] == 1500
        assert s["min"] == 100
        assert s["avg"] == 700

    def test_empty(self):
        assert sm.summarize([]) == {"count": 0}


class TestMatchTask:
    def test_matches_by_time(self):
        st = sm.WorkStat(published_at="2026-09-18 10:03", play_count=488, tags=[])
        tasks = [{"id": 1, "published_at": "2026-09-18T10:03:00", "tags": "[]"}]
        m = sm.match_task(st, tasks)
        assert m and m["id"] == 1

    def test_rejects_far_time(self):
        st = sm.WorkStat(published_at="2026-09-18 10:03", play_count=488, tags=[])
        tasks = [{"id": 1, "published_at": "2026-08-01T10:03:00", "tags": "[]"}]
        assert sm.match_task(st, tasks) is None

    def test_tag_overlap_wins_over_closer_time(self):
        """同平台连发时，话题交集应压过时间接近度。"""
        st = sm.WorkStat(published_at="2026-09-18 10:03", play_count=488,
                         tags=["无声视频", "神反转"])
        tasks = [
            {"id": 1, "published_at": "2026-09-18T10:02:00", "tags": '["无声视频"]'},
            {"id": 2, "published_at": "2026-09-18T10:05:00",
             "tags": '["无声视频", "神反转"]'},
        ]
        m = sm.match_task(st, tasks)
        assert m["id"] == 2, "话题交集更高的应优先匹配"

    def test_unparseable_date_returns_none(self):
        st = sm.WorkStat(published_at="", play_count=1)
        assert sm.match_task(st, [{"id": 1, "published_at": "x"}]) is None


class TestParseDt:
    def test_iso_and_chinese(self):
        assert sm._parse_dt("2026-09-18 10:03") is not None
        assert sm._parse_dt("2026-09-18T10:03") is not None
        assert sm._parse_dt("2026年09月18日 10:03") is not None

    def test_iso_with_seconds_and_microseconds(self):
        """tasks.published_at 实际存的是 'T...:00' 带秒格式。

        回归：旧实现只认 '%Y-%m-%d %H:%M'，解析不了带秒的 ISO 串，
        导致 match_task 恒返回 None、播放量一条都回写不进去。
        """
        assert sm._parse_dt("2026-09-18T10:03:00") is not None
        assert sm._parse_dt("2026-09-18T10:03:00.123456") is not None

    def test_iso_t_matches_plain(self):
        """'T' 分隔与空格分隔应解析成同一时刻。"""
        a = sm._parse_dt("2026-09-18T10:03:00")
        b = sm._parse_dt("2026-09-18 10:03")
        assert a is not None and b is not None
        assert a.hour == b.hour and a.day == b.day

    def test_garbage(self):
        assert sm._parse_dt("") is None
        assert sm._parse_dt("not a date") is None

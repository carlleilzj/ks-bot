"""发现层 + 审核闸门单测。

不联网：用 mock 数据测试 FilterChain 规则、build_adapters 构造、
DB 的 CANDIDATE/PENDING_REVIEW/approve/reject 流程。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# 让 pytest 能 import bot 包（无 __init__.py 在 tests/ 时）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.source.discovery import Candidate, YouTubeSearchAdapter, RSSAdapter, build_adapters
from bot.source.filter import FilterChain, FilterRules
from bot.config import DiscoveryConfig
from bot.db import Database, State


# ============================================================
# FilterChain 单测
# ============================================================

def _make_cand(**kw) -> Candidate:
    base = dict(url="https://youtube.com/watch?v=abc", platform="youtube",
                video_id="abc", title="anime short clip", uploader="studio",
                duration=30, thumbnail="", score=5000,
                reason="YouTube 搜索: anime short", source_tag="yt_search:anime short")
    base.update(kw)
    return Candidate(**base)


def test_filter_pass_within_duration():
    chain = FilterChain(FilterRules.from_dict({"min_duration": 5, "max_duration": 120}))
    ok, _ = chain.keep(_make_cand(duration=30))
    assert ok


def test_filter_reject_too_long():
    chain = FilterChain(FilterRules.from_dict({"min_duration": 5, "max_duration": 120}))
    ok, reason = chain.keep(_make_cand(duration=300))
    assert not ok
    assert "时长" in reason


def test_filter_reject_blacklist_keyword():
    rules = FilterRules.from_dict({"keyword_blacklist": ["reaction"]})
    chain = FilterChain(rules)
    ok, reason = chain.keep(_make_cand(title="anime reaction clip"))
    assert not ok
    assert "黑名单" in reason


def test_filter_require_whitelist():
    rules = FilterRules.from_dict({"keyword_whitelist": ["anime", "animation"]})
    chain = FilterChain(rules)
    ok, _ = chain.keep(_make_cand(title="anime short"))
    assert ok
    # reason 也不能含白名单词，否则会误命中
    ok2, reason2 = chain.keep(_make_cand(title="cooking show", reason="YouTube 搜索: food"))
    assert not ok2
    assert "白名单" in reason2


def test_filter_reject_low_score():
    rules = FilterRules.from_dict({"min_score": 1000})
    chain = FilterChain(rules)
    ok, reason = chain.keep(_make_cand(score=100))
    assert not ok
    assert "热度" in reason


def test_filter_pass_rss_unknown_duration():
    """RSS 候选 duration=0，应放行（不卡时长）。"""
    chain = FilterChain(FilterRules.from_dict({"min_duration": 5, "max_duration": 120}))
    ok, _ = chain.keep(_make_cand(duration=0, score=0))
    assert ok


# ============================================================
# build_adapters 单测
# ============================================================

class _FakeSettings:
    def __init__(self, disc):
        self.discovery = disc


def test_build_adapters_empty_when_disabled():
    s = _FakeSettings(DiscoveryConfig(enabled=False))
    assert build_adapters(s) == []


def test_build_adapters_youtube_search():
    disc = DiscoveryConfig(enabled=True,
                            sources=[{"type": "youtube_search", "queries": ["anime short"]}])
    s = _FakeSettings(disc)
    adapters = build_adapters(s)
    assert len(adapters) == 1
    assert isinstance(adapters[0], YouTubeSearchAdapter)


def test_build_adapters_rss():
    disc = DiscoveryConfig(enabled=True,
                            sources=[{"type": "youtube_rss", "channel_ids": ["UCxxx"]}])
    s = _FakeSettings(disc)
    adapters = build_adapters(s)
    assert len(adapters) == 1
    assert isinstance(adapters[0], RSSAdapter)


def test_build_adapters_mixed():
    disc = DiscoveryConfig(enabled=True, sources=[
        {"type": "youtube_search", "queries": ["a"]},
        {"type": "youtube_rss", "channel_ids": ["UC1"]},
        {"type": "youtube_playlist", "playlist_id": "PLxxx"},
    ])
    s = _FakeSettings(disc)
    adapters = build_adapters(s)
    assert len(adapters) == 3


def test_build_adapters_unknown_type_skipped():
    disc = DiscoveryConfig(enabled=True, sources=[{"type": "unknown_source"}])
    s = _FakeSettings(disc)
    assert build_adapters(s) == []


# ============================================================
# DB 审核流程单测
# ============================================================

class _FakeMeta:
    """最小 VideoMeta 替身，满足 insert_video 所需字段。"""
    def __init__(self, shortcode="yt_abc", platform="youtube", url="https://youtube.com/watch?v=abc"):
        self.source_url = url
        self.platform = platform
        self.video_id = shortcode.split("_")[-1]
        self.shortcode = shortcode
        self.username = "studio"
        self.title = "anime clip"
        self.caption = ""
        self.thumbnail_url = ""
        self.duration = 0
        self.permalink = url


@pytest.fixture()
def db():
    with tempfile.TemporaryDirectory() as d:
        yield Database(Path(d) / "test.db")


def test_insert_candidate_state(db):
    meta = _FakeMeta()
    tid = db.insert_video(meta, state=State.CANDIDATE, source_tag="yt_search:anime short")
    assert tid is not None
    t = db.get(tid)
    assert t["state"] == State.CANDIDATE
    assert t["source_tag"] == "yt_search:anime short"


def test_approve_transitions_to_detected(db):
    meta = _FakeMeta()
    tid = db.insert_video(meta, state=State.CANDIDATE)
    assert db.approve(tid) is True
    assert db.get(tid)["state"] == State.DETECTED


def test_approve_with_platforms(db):
    meta = _FakeMeta()
    tid = db.insert_video(meta, state=State.PENDING_REVIEW)
    assert db.approve(tid, target_platforms=["douyin", "xhs"]) is True
    t = db.get(tid)
    assert t["state"] == State.DETECTED
    assert t["target_platforms"] == "douyin,xhs"


def test_approve_rejects_non_review_state(db):
    meta = _FakeMeta()
    tid = db.insert_video(meta, state=State.DETECTED)  # 已在流水线中
    assert db.approve(tid) is False


def test_reject_transitions_to_skipped(db):
    meta = _FakeMeta()
    tid = db.insert_video(meta, state=State.PENDING_REVIEW)
    assert db.reject(tid, "不合适") is True
    t = db.get(tid)
    assert t["state"] == State.SKIPPED
    assert "不合适" in (t["error"] or "")


def test_pending_review_count(db):
    assert db.pending_review_count() == 0
    db.insert_video(_FakeMeta("yt_a", url="https://youtube.com/watch?v=a"), state=State.CANDIDATE)
    db.insert_video(_FakeMeta("yt_b", url="https://youtube.com/watch?v=b"), state=State.PENDING_REVIEW)
    db.insert_video(_FakeMeta("yt_c", url="https://youtube.com/watch?v=c"), state=State.DETECTED)  # 不计
    assert db.pending_review_count() == 2


def test_dedup_by_source_url(db):
    """同一 source_url 二次插入返回 None（UNIQUE 约束）。"""
    meta = _FakeMeta()
    assert db.insert_video(meta, state=State.CANDIDATE) is not None
    assert db.insert_video(meta, state=State.CANDIDATE) is None


def test_find_by_source_url(db):
    meta = _FakeMeta()
    db.insert_video(meta, state=State.CANDIDATE)
    found = db.find_by_source_url(meta.source_url)
    assert found is not None
    assert found["state"] == State.CANDIDATE

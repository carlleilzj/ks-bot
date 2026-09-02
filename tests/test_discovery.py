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

from bot.config import DiscoveryConfig, Settings
from bot.db import Database, State
from bot.source.discovery import (
    Candidate,
    ChannelAdapter,
    YouTubeSearchAdapter,
    build_adapters,
)
from bot.source.filter import FilterChain, FilterRules
from bot.source.scheduler import DiscoveryScheduler

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


def test_filter_rss_negative_score_skips_threshold():
    """RSS 候选 score 用负数序数（0,-1,-2…越新越大），不参与热度阈值判定。"""
    chain = FilterChain(FilterRules.from_dict({"min_score": 1000}))
    for idx_score in (0, -1, -4, -9):
        ok, reason = chain.keep(_make_cand(score=idx_score, title="anime pv"))
        assert ok, f"score={idx_score} 应放行: {reason}"


def test_filter_trusted_source_skips_whitelist():
    """官方频道来源（trusted_source）跳过白名单；黑名单仍然生效。"""
    rules = FilterRules.from_dict({"keyword_whitelist": ["anime"]})
    chain = FilterChain(rules)
    # 标题无 anime 字样的官方频道 PV → 放行
    ok, _ = chain.keep(_make_cand(title="《KAGURABACHI》 - Character PV",
                                  reason="YouTube RSS: UCxxx", trusted_source=True))
    assert ok
    # 同样标题但来源不可信 → 拦
    ok2, reason2 = chain.keep(_make_cand(title="《KAGURABACHI》 - Character PV",
                                         reason="YouTube RSS: UCxxx"))
    assert not ok2
    assert "白名单" in reason2
    # 可信来源但命中黑名单 → 仍然拦
    rules_bl = FilterRules.from_dict({"keyword_blacklist": ["recap"]})
    chain_bl = FilterChain(rules_bl)
    ok3, reason3 = chain_bl.keep(_make_cand(title="Anime Recap 全集解说",
                                            reason="YouTube RSS: UCxxx",
                                            trusted_source=True))
    assert not ok3
    assert "黑名单" in reason3


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
    """youtube_rss 旧类型名自动升级为 ChannelAdapter（RSS 端点被部分出口 404）。"""
    disc = DiscoveryConfig(enabled=True,
                            sources=[{"type": "youtube_rss", "channel_ids": ["UCxxx"]}])
    s = _FakeSettings(disc)
    adapters = build_adapters(s)
    assert len(adapters) == 1
    assert isinstance(adapters[0], ChannelAdapter)
    assert adapters[0].channels == ["UCxxx"]
    assert adapters[0].tab == "shorts"


def test_build_adapters_channel():
    """youtube_channel 新类型：channels + tab 参数。"""
    disc = DiscoveryConfig(enabled=True,
                            sources=[{"type": "youtube_channel",
                                      "channels": ["UCabc", "@somechannel"],
                                      "tab": "videos"}])
    s = _FakeSettings(disc)
    adapters = build_adapters(s)
    assert len(adapters) == 1
    assert isinstance(adapters[0], ChannelAdapter)
    assert adapters[0].channels == ["UCabc", "@somechannel"]
    assert adapters[0].tab == "videos"


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
    def __init__(self, shortcode="yt_abc", platform="youtube",
                 url="https://youtube.com/watch?v=abc", thumbnail=""):
        self.source_url = url
        self.platform = platform
        self.video_id = shortcode.split("_")[-1]
        self.shortcode = shortcode
        self.username = "studio"
        self.title = "anime clip"
        self.caption = ""
        self.thumbnail_url = thumbnail
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


# ============================================================
# 真人检测反向过滤（scheduler 集成，全 mock 不联网）
# ============================================================

class _FakeAdapter:
    """产出固定候选的假适配器。"""
    name = "fake"

    def __init__(self, cands):
        self._cands = cands

    def discover(self, limit=20):
        return list(self._cands)


def _sched(db, cands, reject_real_person=True) -> DiscoveryScheduler:
    """构造一个可跑 _cycle 的 scheduler：假适配器 + mock 掉网络/AI。"""
    s = Settings()
    s.ai_api_key = "test-key"  # 让 vision 的调用路径可达（会被 mock）
    disc = DiscoveryConfig(enabled=True,
                           sources=[{"type": "youtube_search", "queries": ["q"]}],
                           filters={"reject_real_person": reject_real_person,
                                    "min_score": 0, "keyword_whitelist": None,
                                    "keyword_blacklist": None})
    s.discovery = disc
    sched = DiscoveryScheduler(s, db)
    sched.adapters = [_FakeAdapter(cands)]
    sched.enabled = True
    # mock 网络/AI：封面直接生成 1x1 jpeg、元数据用假 meta、TG 发送为空操作
    sched._download_cover = lambda url, dest: dest.write_bytes(b"\xff\xd8fake")
    return sched


def _cand(vid="v1", title="anime short", score=5000) -> Candidate:
    return Candidate(url=f"https://youtube.com/watch?v={vid}", platform="youtube",
                     video_id=vid, title=title, uploader="studio", duration=30,
                     thumbnail="http://x/y.jpg", score=score,
                     reason="yt_search:anime", source_tag="yt_search:anime")


@pytest.fixture()
def patches(monkeypatch):
    """mock extract_meta / telegram / inspect_cover，按需调整返回值。"""
    import bot.source.scheduler as sched_mod
    from bot.ai.vision import CoverVerdict
    state = {"real_person": False, "watermark": False, "is_animation": True,
             "raise_error": False}

    monkeypatch.setattr(sched_mod, "extract_meta",
                        lambda url: _FakeMeta(shortcode=f"yt_{abs(hash(url)) % 100000}",
                                              url=url, thumbnail="http://x/y.jpg"))
    monkeypatch.setattr(sched_mod.telegram, "send_review_card", lambda s, task: None)
    monkeypatch.setattr(sched_mod.telegram, "notify_info", lambda s, text: None)

    def fake_inspect(cover, settings):
        if state.get("raise_error"):
            raise RuntimeError("AI 接口超时")
        return CoverVerdict(
            is_animation=state["is_animation"],
            has_real_person=state["real_person"],
            has_watermark=state["watermark"],
            watermark_desc="右下角 TikTok logo" if state["watermark"] else "",
            reason="mock",
        )

    monkeypatch.setattr("bot.ai.vision.inspect_cover", fake_inspect)
    return state


def test_real_person_rejected(db, patches):
    """封面检测到真人 → 直接 SKIPPED，不发审核卡片。"""
    patches["real_person"] = True  # 检测到真人
    sched = _sched(db, [_cand("v1")])
    sched._cycle()
    assert db.pending_review_count() == 0
    tasks = db.recent(1)
    assert tasks[0]["state"] == State.SKIPPED
    assert "真人" in (tasks[0]["error"] or "")


def test_watermark_rejected(db, patches):
    """封面检测到水印 → SKIPPED（动物动画赛道要求无水印）。"""
    patches["watermark"] = True
    sched = _sched(db, [_cand("v1")])
    sched._cycle()
    assert db.pending_review_count() == 0
    tasks = db.recent(1)
    assert tasks[0]["state"] == State.SKIPPED
    assert "水印" in (tasks[0]["error"] or "")


def test_not_animation_rejected(db, patches):
    """封面明确非动画（真人实拍等）→ SKIPPED。"""
    patches["is_animation"] = False
    sched = _sched(db, [_cand("v1")])
    sched._cycle()
    assert db.pending_review_count() == 0
    tasks = db.recent(1)
    assert tasks[0]["state"] == State.SKIPPED
    assert "动画" in (tasks[0]["error"] or "")


def test_anime_passed_to_review(db, patches):
    """封面干净（动画+非真人+无水印）→ 正常进入 PENDING_REVIEW。"""
    sched = _sched(db, [_cand("v1")])
    sched._cycle()
    assert db.pending_review_count() == 1


def test_check_error_passes_through(db, patches):
    """检测抛异常 → 不误杀，放行进入审核。"""
    patches["raise_error"] = True
    sched = _sched(db, [_cand("v1")])
    sched._cycle()
    assert db.pending_review_count() == 1


def test_disabled_flag_skips_check(db, patches, monkeypatch):
    """reject_real_person=False → 完全不调检测，直接进审核。"""
    called = {"n": 0}
    monkeypatch.setattr("bot.ai.vision.inspect_cover",
                        lambda c, s: called.__setitem__("n", called["n"] + 1))
    sched = _sched(db, [_cand("v1")], reject_real_person=False)
    sched._cycle()
    assert called["n"] == 0
    assert db.pending_review_count() == 1

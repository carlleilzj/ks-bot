"""测试爆款源账号自动发掘与晋级机制。"""
from __future__ import annotations

import pytest
from bot.db import Database, now_iso
from bot.source import source_accounts as sa


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "ACCOUNTS_FILE", tmp_path / "src_promote.json")


def _seed_job_with_play(db: Database, lane: str, username: str, play: int, title: str = "t", caption: str = ""):
    ts = now_iso()
    with db._lock:
        cur = db.conn.execute(
            """INSERT INTO tasks (ig_media_id, shortcode, state, published_at,
                                  created_at, updated_at, username, play_count,
                                  title, caption, source_platform, source_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"m_{username}_{play}", f"sc_{username}", "NOTIFIED", ts, ts, ts,
             username, play, title, caption, "instagram", f"https://www.instagram.com/reel/{username}/"),
        )
        tid = cur.lastrowid
        db.conn.execute(
            """INSERT INTO publish_jobs (task_id, platform, state, published_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (tid, lane, "PUBLISHED", ts, ts, ts),
        )
        db.conn.commit()
    return tid


class _FakePlat:
    def __init__(self, discover):
        self.discover = discover


class _FakeSettings:
    def __init__(self, platforms):
        self.platforms = platforms


def test_promotes_burst_account_above_threshold(tmp_path):
    db = Database(path=tmp_path / "t.db")
    s = _FakeSettings({
        "kuaishou": _FakePlat({"enabled": True, "require_any": []}),
    })
    _seed_job_with_play(db, "kuaishou", "burst_creator", 5000, "大爆款视频")
    
    promoted = sa.promote_burst_accounts(db, s=s, threshold_play=2500)
    assert len(promoted) == 1
    assert promoted[0]["account"] == "burst_creator"
    assert promoted[0]["play_count"] == 5000
    
    # 再次运行应幂等，不重复晋级
    promoted2 = sa.promote_burst_accounts(db, s=s, threshold_play=2500)
    assert len(promoted2) == 0


def test_ignores_below_threshold(tmp_path):
    db = Database(path=tmp_path / "t.db")
    s = _FakeSettings({"kuaishou": _FakePlat({"enabled": True})})
    _seed_job_with_play(db, "kuaishou", "low_creator", 800)
    
    promoted = sa.promote_burst_accounts(db, s=s, threshold_play=2500)
    assert len(promoted) == 0


def test_b_lane_enforces_genre_gate(tmp_path):
    """B 号必须命中床品/睡衣题材才允许晋级，防止非家居爆款污染。"""
    db = Database(path=tmp_path / "t.db")
    s = _FakeSettings({
        "kuaishou2": _FakePlat({"enabled": True, "require_any": ["四件套", "bedding"]}),
    })
    # 爆款但是内容是无关的美妆
    _seed_job_with_play(db, "kuaishou2", "makeup_star", 10000, "修眉教程", "修眉好物")
    # 爆款且确实是床品
    _seed_job_with_play(db, "kuaishou2", "home_star", 5000, "纯棉四件套测评", "bedding aesthetic")
    
    promoted = sa.promote_burst_accounts(db, s=s, threshold_play=2500)
    assert len(promoted) == 1
    assert promoted[0]["account"] == "home_star"

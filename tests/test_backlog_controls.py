"""回归测试：待发水位闸门 + READY 超期清理（2026-11-12 积压治理）。

背景数据（生产实测）：
  发布产能 = 快手 A/B 各 daily_limit 6 条 = 12 条/天
  发现层入库 = 约 21-31 条/天（11-10 单日 154 条）
  → 净积压约 +15/天，READY 涨到 32 条，最老的等了 2 天，素材已过时效

两条治理手段，各自锁住语义：
  1. 水位闸门：待发量 >= backlog_watermark 时 run_discovery 整轮跳过，
     消化后自动恢复；force=True 仍可绕过（人工补素材）。
  2. 超期清理：READY 排队超过 3 天的任务标 SKIPPED 并删媒体，释放水位名额。
"""

from __future__ import annotations

import pytest

import bot.source.discover as disc
from bot.config import Settings
from bot.db import Database, State
from bot.maintenance import cleanup


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "backlog.db")


def _add_ready(db: Database, n: int, created_at: str = "2026-11-01T00:00:00") -> list[int]:
    ids = []
    for _ in range(n):
        cur = db.conn.execute(
            "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
            "VALUES (lower(hex(randomblob(8))), lower(hex(randomblob(4))), ?, ?, ?)",
            (State.READY, created_at, created_at))
        ids.append(cur.lastrowid)
    db.conn.commit()
    return ids


# ---------- 1) 待发水位计数 ----------

def test_backlog_counts_ready_tasks(tmp_path):
    db = _make_db(tmp_path)
    _add_ready(db, 5)
    assert db.backlog_size() == 5
    db.close()


def test_backlog_does_not_count_work_in_progress(tmp_path):
    """DETECTED/DOWNLOADED 是在制品，不算库存——否则流水线一忙闸门就误关。"""
    db = _make_db(tmp_path)
    for state in (State.DETECTED, State.DOWNLOADED, State.TRANSCODED):
        db.conn.execute(
            "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (state, state.lower(), state, "2026-11-01T00:00:00", "2026-11-01T00:00:00"))
    db.conn.commit()
    _add_ready(db, 2)
    assert db.backlog_size() == 2
    db.close()


def test_backlog_takes_max_not_sum(tmp_path):
    """READY 任务与其 PENDING job 是同一批东西的两个视角，相加会翻倍。"""
    db = _make_db(tmp_path)
    ids = _add_ready(db, 3)
    for tid in ids:
        db.create_publish_jobs(tid, ["kuaishou"])
    # 3 个 READY + 3 个 PENDING job，水位应为 3 而不是 6
    assert db.backlog_size() == 3
    db.close()


# ---------- 2) 水位闸门 ----------

@pytest.fixture()
def gate_env(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setattr(disc, "_discovery_due", lambda s: True)
    # 任何真实发现动作都是失败信号：闸门放行时才该发生
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(disc, "lane_from_config", lambda *a, **k: _boom())
    yield db, called
    db.close()


def _settings(watermark=24) -> Settings:
    s = Settings()
    s.discovery = {"backlog_watermark": watermark}
    return s


def test_discovery_skipped_at_watermark(tmp_path, monkeypatch):
    """到达水位 → 整轮跳过，不做任何寻源。

    注意这里必须配一个**真实 lane**：早先版本留空 platforms，平台循环根本不执行，
    lane 自然不会被调用 —— 断言恒成立，闸门即使失效测试也照样绿（实测踩到）。
    """
    db = _make_db(tmp_path)
    _add_ready(db, 24)  # 正好等于阈值 24
    monkeypatch.setattr(disc, "_discovery_due", lambda s: True)

    touched = {"n": 0}

    def _lane(name, conf):
        touched["n"] += 1
        return disc.LaneConfig(name=name, per_run=1)

    monkeypatch.setattr(disc, "lane_from_config", _lane)
    monkeypatch.setattr(disc, "run_whitelist", lambda *a, **k: [])

    s = _settings(24)
    s.platforms = _one_lane()
    assert disc.run_discovery(s, db) == {}
    assert touched["n"] == 0, "到达水位后不应再进入任何 lane"
    db.close()


def test_discovery_runs_below_watermark(tmp_path, monkeypatch):
    """水位以下 → 正常发现（闸门不能把发现层关死）。"""
    db = _make_db(tmp_path)
    _add_ready(db, 23)
    monkeypatch.setattr(disc, "_discovery_due", lambda s: True)

    reached = {"n": 0}
    from bot.source.discover import LaneConfig

    def _lane(*a, **k):
        reached["n"] += 1            # 走到这里 = 闸门已放行
        return LaneConfig(name=a[0] if a else "kuaishou", per_run=1)

    monkeypatch.setattr(disc, "lane_from_config", _lane)
    monkeypatch.setattr(disc, "run_whitelist", lambda *a, **k: [])

    s = _settings(24)
    s.platforms = _one_lane()   # 有真实 lane，才能观察闸门是否放行
    disc.run_discovery(s, db)
    assert reached["n"] == 1, "水位以下必须放行到 lane（闸门不能把发现层关死）"
    db.close()


def _one_lane():
    """构造一个配置了 discover 的平台，使 run_discovery 会走到 lane_from_config。"""
    from bot.config import PlatformConfig

    return {"kuaishou": PlatformConfig(
        enabled=True, discover={"enabled": True, "ig_tags": ["3danimation"]})}


def test_force_bypasses_watermark(tmp_path, monkeypatch):
    """force=True（人工补素材）必须能越过闸门。"""
    db = _make_db(tmp_path)
    _add_ready(db, 100)  # 远超水位
    monkeypatch.setattr(disc, "_discovery_due", lambda s: True)

    reached = {"n": 0}
    from bot.source.discover import LaneConfig

    def _lane(*a, **k):
        reached["n"] += 1
        return LaneConfig(name=a[0] if a else "kuaishou", per_run=1)

    monkeypatch.setattr(disc, "lane_from_config", _lane)
    monkeypatch.setattr(disc, "run_whitelist", lambda *a, **k: [])

    s = _settings(24)
    s.platforms = _one_lane()
    disc.run_discovery(s, db, force=True)
    assert reached["n"] == 1, "force=True 必须绕过水位闸门"
    db.close()


def test_watermark_zero_disables_gate(tmp_path, monkeypatch):
    """backlog_watermark=0 → 关闭闸门（保留旧行为）。"""
    db = _make_db(tmp_path)
    _add_ready(db, 100)
    monkeypatch.setattr(disc, "_discovery_due", lambda s: True)
    assert disc.backlog_watermark(_settings(0)) == 0
    # 0 时不应因水位跳过
    assert disc.run_discovery(_settings(0), db) == {}
    db.close()


def test_watermark_defaults_to_24_without_config():
    """未配置时回退默认 24（不能因为配置漏写就悄悄关掉闸门）。"""
    assert disc.backlog_watermark(Settings()) == 24


def test_gate_survives_db_error(tmp_path, monkeypatch):
    """读水位失败时保守继续发现，不能把发现层意外关死。"""
    class BrokenDB:
        def backlog_size(self):
            raise RuntimeError("db locked")

    monkeypatch.setattr(disc, "_discovery_due", lambda s: True)
    monkeypatch.setattr(disc, "lane_from_config",
                        lambda *a, **k: {})
    s = _settings(24)
    s.platforms = {}
    assert disc.run_discovery(s, BrokenDB()) == {}


# ---------- 3) READY 超期清理 ----------

def test_expired_ready_selects_only_old_ready(tmp_path):
    db = _make_db(tmp_path)
    _add_ready(db, 2, created_at="2026-01-01T00:00:00")          # 很老
    fresh = _add_ready(db, 1, created_at="2099-01-01T00:00:00")  # 未来（新）
    expired = db.expired_ready(3)
    assert len(expired) == 2
    assert fresh[0] not in [t["id"] for t in expired]
    db.close()


def test_expired_ready_never_touches_published(tmp_path):
    """只有 READY 会被清理；已发布/处理中的任务绝不能被误标 SKIPPED。"""
    db = _make_db(tmp_path)
    for i, state in enumerate((State.NOTIFIED, State.DETECTED, State.SUBTITLED)):
        db.conn.execute(
            "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (f"x{i}", f"x{i}", state, "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    db.conn.commit()
    _add_ready(db, 1, created_at="2026-01-01T00:00:00")
    expired = db.expired_ready(3)
    assert len(expired) == 1
    assert expired[0]["state"] == State.READY
    db.close()


def test_expire_stale_ready_marks_skipped_and_frees_backlog(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    ids = _add_ready(db, 3, created_at="2026-01-01T00:00:00")
    for tid in ids:
        db.create_publish_jobs(tid, ["kuaishou"])

    stats = cleanup.expire_stale_ready(db, days=3)

    assert stats["expired"] == 3
    for tid in ids:
        task = db.get(tid)
        assert task["state"] == State.SKIPPED
        assert "排队超过" in (task["error"] or "")
    assert db.backlog_size() == 0, "过期清理后水位必须降下来，否则闸门永远不开"
    db.close()


def test_expire_keeps_recent_ready(tmp_path):
    """刚入库的 READY 不能被清掉。"""
    db = _make_db(tmp_path)
    db.conn.execute(
        "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
        "VALUES ('n','new',?, datetime('now','localtime'), datetime('now','localtime'))",
        (State.READY,))
    db.conn.commit()

    assert cleanup.expire_stale_ready(db, days=3)["expired"] == 0
    assert db.expired_ready(3) == []
    db.close()


def test_run_cleanup_invokes_expiry(tmp_path, monkeypatch):
    """run_cleanup 必须真的调用超期清理（否则线上永远不会触发）。"""
    db = _make_db(tmp_path)
    _add_ready(db, 1, created_at="2026-01-01T00:00:00")

    monkeypatch.setattr(cleanup, "cleanup_screenshots", lambda: 0)
    monkeypatch.setattr(cleanup, "purge_orphans",
                        lambda d: {"deleted": 0, "freed_mb": 0})
    monkeypatch.setattr(cleanup, "rotate_logs", lambda: None)

    cleanup.run_cleanup(db)
    assert db.get(1)["state"] == State.SKIPPED
    db.close()

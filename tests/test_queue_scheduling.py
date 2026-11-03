"""回归测试：队列调度与任务状态自愈（2026-11-04 深度审计修复）。"""

from __future__ import annotations

import pytest

from bot.db import Database, JobState, State


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def _make_task(db, n: int, state=State.READY, day: int = 1) -> int:
    cur = db.conn.execute(
        """INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (f"m{n}", f"sc{n}", state,
         f"2026-01-{day:02d}T00:00:00", f"2026-01-{day:02d}T00:00:00"),
    )
    db.conn.commit()
    return cur.lastrowid


def test_pending_does_not_starve_new_tasks(db):
    """积压的 READY 不能把 LIMIT 名额占满，新的 DETECTED 必须优先被处理。"""
    for i in range(60):
        _make_task(db, i, State.READY, day=1)

    fresh = _make_task(db, 999, State.DETECTED, day=28)
    rows = db.pending(limit=50)

    ids = [r["id"] for r in rows]
    assert fresh in ids, "新入库的 DETECTED 任务被 READY 积压饿死"
    assert rows[0]["id"] == fresh, "处理中的任务应排在 READY 前面"


def test_pending_still_returns_ready_when_no_others(db):
    t = _make_task(db, 1, State.READY)
    assert [r["id"] for r in db.pending()] == [t]


def test_finalize_orphan_ready(db):
    """READY 但所有平台 job 已终态 → 补写 PUBLISHED。"""
    t = _make_task(db, 1, State.READY)
    db.create_publish_jobs(t, ["kuaishou"])
    for j in db.jobs(t):
        db.update_job(j["id"], state=JobState.PUBLISHED, published_at="2026-01-01T10:00:00")

    assert db.finalize_orphan_ready() == 1
    assert db.get(t)["state"] == State.PUBLISHED


def test_finalize_orphan_ready_keeps_pending(db):
    """仍有 PENDING job 的 READY 不能被误判为完成。"""
    t = _make_task(db, 1, State.READY)
    db.create_publish_jobs(t, ["kuaishou"])
    assert db.finalize_orphan_ready() == 0
    assert db.get(t)["state"] == State.READY


def test_finalize_orphan_ready_ignores_no_jobs(db):
    """没有 job 的 READY（建 job 失败）不动，留给人工排查。"""
    t = _make_task(db, 1, State.READY)
    assert db.finalize_orphan_ready() == 0
    assert db.get(t)["state"] == State.READY

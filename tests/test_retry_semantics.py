"""回归测试：retries 语义 = 同一阶段连续失败次数（2026-11-04 深度审计修复）。

历史问题：retries 全生命周期累计、阶段成功不清零，任意 3 次瞬时错误
（下载抖动 + ASR 超时 + 发布异常）就把任务打成 FAILED 终态。
"""

from __future__ import annotations

import pytest

import bot.main as main_mod
from bot.config import Settings
from bot.db import Database, State


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = Database(tmp_path / "r.db")
    cur = db.conn.execute(
        "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
        "VALUES ('m1','sc1',?, '2026-01-01T00:00:00','2026-01-01T00:00:00')",
        (State.DOWNLOADED,))
    db.conn.commit()
    tid = cur.lastrowid
    monkeypatch.setattr(main_mod.telegram, "notify_info", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.telegram, "notify_failed", lambda *a, **k: None)
    yield db, tid, Settings()
    db.close()


def test_stage_success_resets_retries(env, monkeypatch):
    """历史遗留的 retries（来自之前阶段）在阶段推进成功后必须清零。

    否则任意 3 次分布于不同阶段的瞬时错误会把任务打成 FAILED 终态。
    """
    db, tid, s = env
    db.update(tid, retries=2)   # 前面阶段累计的失败次数

    def advance_step(s_, db_, task_):
        db_.update(task_["id"], state=State.TRANSCODED)

    monkeypatch.setattr(main_mod, "step_transcode", advance_step)
    monkeypatch.setattr(main_mod, "step_transcribe", lambda *a, **k: None)

    main_mod.advance_task(s, db, tid, dry_run=False, headed=False, stage="process")
    row = db.get(tid)
    assert row["state"] == State.TRANSCODED
    assert row["retries"] == 0, f"阶段成功后应清零 retries，实际 {row['retries']}"


def test_same_stage_failures_still_reach_max(env, monkeypatch):
    """同一阶段连续失败仍然会到上限并判 FAILED（不能因为修复而失去止损）。"""
    db, tid, s = env

    def always_fail(s_, db_, task_):
        raise RuntimeError("持续失败")

    monkeypatch.setattr(main_mod, "step_transcode", always_fail)
    for _ in range(main_mod.MAX_RETRIES):
        main_mod.advance_task(s, db, tid, dry_run=False, headed=False, stage="process")
    row = db.get(tid)
    assert row["state"] == State.FAILED
    assert row["retries"] >= main_mod.MAX_RETRIES


def test_no_infinite_spin_when_state_unchanged(env, monkeypatch):
    """状态不前进（发布窗口未开）时必须立即返回，不能空转 12 次。"""
    db, tid, s = env
    calls = {"n": 0}
    db.update(tid, state=State.READY)

    def noop_publish(*a, **k):
        calls["n"] += 1

    monkeypatch.setattr(main_mod, "_window_only_gate", lambda s_, db_: "不在发布窗口")
    monkeypatch.setattr(main_mod, "step_publish", noop_publish)

    main_mod.advance_task(s, db, tid, dry_run=False, headed=False, stage="all")
    assert calls["n"] == 0

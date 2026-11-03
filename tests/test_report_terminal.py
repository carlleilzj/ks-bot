"""回归测试：远端回报的终态收敛（2026-11-04 深度审计修复）。

背景：远端以前没有失败终态出口 —— retries 涨到上限后 worker 只是跳过，
job 永远 PENDING、任务永远 READY，既不发布也不通知。
登录态失效则无限空转、每轮刷一条 TG。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bot.db import Database, JobState, State
from bot.remote_api import RemoteApi


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "rt.db")
    cur = db.conn.execute(
        "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
        "VALUES ('m1','sc1',?, '2026-01-01T00:00:00','2026-01-01T00:00:00')",
        (State.READY,))
    db.conn.commit()
    tid = cur.lastrowid
    db.create_publish_jobs(tid, ["kuaishou"])
    api = RemoteApi(MagicMock(), db, lambda s, db, p: None)
    yield api, db, tid
    db.close()


def _claim(db, tid):
    """把 job 置为 PUBLISHING（模拟 worker 认领）。"""
    job = db.jobs(tid)[0]
    db.update_job(job["id"], state="PUBLISHING", claimed_at="2026-01-01T10:00:00")
    return job["id"]


def _job(db, tid):
    return db.jobs(tid)[0]


def test_failure_converts_to_failed_after_max_retries(env):
    api, db, tid = env
    for i in range(6):
        _claim(db, tid)
        api.report({"task_id": tid, "platform": "kuaishou",
                    "ok": False, "error": f"网络抖动 {i}"})
        job = _job(db, tid)
        if job["state"] == JobState.FAILED:
            break
    job = _job(db, tid)
    assert job["state"] == "FAILED", f"重试上限后应有终态，实际 {job['state']}"
    assert job["retries"] >= 5


def test_failure_before_limit_stays_pending(env):
    api, db, tid = env
    _claim(db, tid)
    api.report({"task_id": tid, "platform": "kuaishou", "ok": False, "error": "x"})
    job = _job(db, tid)
    assert job["state"] == "PENDING"
    assert job["retries"] == 1


def test_login_expired_does_not_consume_retries(env):
    api, db, tid = env
    _claim(db, tid)
    api.report({"task_id": tid, "platform": "kuaishou", "ok": False,
                "error": "登录态失效", "login_expired": True})
    job = _job(db, tid)
    assert job["state"] == "PENDING"
    assert job["retries"] == 0, "登录失效不是任务本身的错，不该烧重试次数"


def test_repeated_login_expired_eventually_skips(env):
    """连续登录失效不能无限空转 —— 第 2 次后转 SKIPPED 等人工扫码。"""
    api, db, tid = env
    states = []
    for _ in range(3):
        _claim(db, tid)
        api.report({"task_id": tid, "platform": "kuaishou", "ok": False,
                    "error": "登录态失效", "login_expired": True})
        states.append(_job(db, tid)["state"])
    assert states[0] == "PENDING"
    assert states[1] == "SKIPPED", f"连续第 2 次登录失效应止损，实际序列 {states}"


def test_unrecoverable_login_skips_immediately(env):
    api, db, tid = env
    _claim(db, tid)
    api.report({"task_id": tid, "platform": "kuaishou", "ok": False,
                "error": "账号被封", "login_expired": True, "unrecoverable": True})
    assert _job(db, tid)["state"] == "SKIPPED"


def test_success_marks_published(env):
    api, db, tid = env
    _claim(db, tid)
    api.report({"task_id": tid, "platform": "kuaishou", "ok": True,
                "url": "https://www.kuaishou.com/short-video/abc"})
    job = _job(db, tid)
    assert job["state"] == "PUBLISHED"
    assert job["url"].endswith("/abc")
    assert db.get(tid)["state"] == State.PUBLISHED


def test_report_without_claiming_is_rejected(env):
    """重复回报幂等：job 已不是 PUBLISHING 时不能改状态。"""
    api, db, tid = env
    res = api.report({"task_id": tid, "platform": "kuaishou", "ok": True})
    assert res["ok"] is False
    assert _job(db, tid)["state"] == "PENDING"

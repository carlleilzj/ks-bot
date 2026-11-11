"""回归测试：发布终态不变量 + worker 回收安全阀（2026-11-12 修复）。

三处历史缺陷，都只在特定时序下才炸，测试必须锁住语义而不是实现：

1. `main.step_publish` 收尾：全部 job 都是 FAILED/SKIPPED 时也会把任务标
   PUBLISHED，随后 step_notify 推「✅ 发布完成」——凭空伪造一条已发布记录。
   不变量（DB.finalize_orphan_ready 与 remote_api.report 都已遵守）：
   **至少一条 job 真的 PUBLISHED 才能收尾。**

2. `publish_worker` 启动路径调用 /api/release_stale 时没带 outbox 排除列表。
   worker 停机 >30 分钟重启时，那些「已发出但成功回报还在 outbox」的 job
   会被退回 PENDING，随后补报发现找不到 PUBLISHING job（200 + ok:false）
   → 条目被丢弃 → **同一条视频被重复发布**。

3. outbox 存在但读不出来时，绝不能「当作空」继续回收——那等于把已发出的
   视频当成没发。必须跳过本轮回收（fail-safe）。
"""

from __future__ import annotations

import json

import pytest

import bot.main as main_mod
import bot.publish_worker as pw
from bot.config import Settings
from bot.db import Database, JobState, State

# ---------- 1) step_publish 收尾不变量 ----------

@pytest.fixture()
def task_env(tmp_path, monkeypatch):
    db = Database(tmp_path / "inv.db")
    cur = db.conn.execute(
        "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
        "VALUES ('m1','sc1',?, '2026-01-01T00:00:00','2026-01-01T00:00:00')",
        (State.SUBTITLED,))
    db.conn.commit()
    tid = cur.lastrowid
    monkeypatch.setattr(main_mod.telegram, "notify_info", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.telegram, "notify_failed", lambda *a, **k: None)
    yield db, tid
    db.close()


def _jobs_all_terminal(db, tid, state: str) -> None:
    db.create_publish_jobs(tid, ["kuaishou", "douyin"])
    for j in db.jobs(tid):
        db.update_job(j["id"], state=state)


def test_all_failed_does_not_mark_published(task_env):
    """所有平台都失败 → 任务不能标 PUBLISHED（否则会推送「发布完成」）。"""
    db, tid = task_env
    _jobs_all_terminal(db, tid, JobState.FAILED)
    task = db.get(tid)

    # step_publish 的收尾逻辑通过 _advance_one_step 走到；这里直接驱动收尾，
    # 避免依赖各平台的 publish() 实现。
    main_mod.step_publish(Settings(), db, task, headed=False)

    after = db.get(tid)
    assert after["state"] == State.SUBTITLED, \
        f"全平台失败不该收尾为 PUBLISHED，实际 {after['state']}"
    assert after["published_at"] is None
    assert after["error"], "应写入可见的 error 供人工处理"


def test_all_skipped_does_not_mark_published(task_env):
    """全部被跳过（如未登录/停用）同样不能算发布完成。"""
    db, tid = task_env
    _jobs_all_terminal(db, tid, JobState.SKIPPED)
    main_mod.step_publish(Settings(), db, db.get(tid), headed=False)

    after = db.get(tid)
    assert after["state"] == State.SUBTITLED
    assert after["published_at"] is None


def test_at_least_one_published_marks_task_published(task_env):
    """有一条真的成功 → 允许收尾（保持原有正常路径）。"""
    db, tid = task_env
    db.create_publish_jobs(tid, ["kuaishou", "douyin"])
    ks, dy = db.jobs(tid)
    db.update_job(ks["id"], state=JobState.PUBLISHED, url="https://k/1",
                  published_at="2026-01-02T10:00:00")
    db.update_job(dy["id"], state=JobState.FAILED)

    main_mod.step_publish(Settings(), db, db.get(tid), headed=False)

    after = db.get(tid)
    assert after["state"] == State.PUBLISHED
    assert after["published_at"]


# ---------- 2) 启动/轮询回收必须共用同一份排除逻辑 ----------

def test_release_body_always_carries_outbox_exclude(tmp_path, monkeypatch):
    """outbox 里的 job 必须出现在 exclude 中，否则重启时会被重复发布。"""
    outbox = tmp_path / "pending_reports.jsonl"
    outbox.write_text(
        json.dumps({"task_id": 42, "platform": "kuaishou"}) + "\n"
        + json.dumps({"task_id": 77, "platform": "kuaishou2"}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(pw, "PENDING_REPORTS_PATH", outbox)

    body = pw.release_stale_json("http://x", Settings(), 30)

    assert body.get("skip") is not True
    assert body["older_than_minutes"] == 30
    assert body["exclude"] == ["42:kuaishou", "77:kuaishou2"]


def test_release_body_has_exclude_key_even_when_empty(tmp_path, monkeypatch):
    """没有 outbox 也要显式带空 exclude：协调端据此跳过 0 条，语义清晰。"""
    monkeypatch.setattr(pw, "PENDING_REPORTS_PATH", tmp_path / "absent.jsonl")

    body = pw.release_stale_json("http://x", Settings(), 30)

    assert body == {"older_than_minutes": 30, "exclude": []}


def test_release_skipped_when_outbox_unreadable(tmp_path, monkeypatch):
    """outbox 读不出来 → 返回 skip，绝不能当成「没有待补报」去回收。"""
    bad = tmp_path / "pending_reports.jsonl"
    bad.mkdir()  # 目录而不是文件：read_text 必然抛错
    monkeypatch.setattr(pw, "PENDING_REPORTS_PATH", bad)

    assert pw.release_stale_json("http://x", Settings(), 30) == {"skip": True}


def test_startup_paths_do_not_inline_release_stale():
    """结构约束：启动/轮询路径不能再各写一份请求体（正是分歧的来源）。

    只允许通过 _release_stale()/release_stale_json() 调用，
    禁止直接 c.post(.../api/release_stale, json={"older_than_minutes": 30})。
    """
    import inspect

    src = inspect.getsource(pw.main)
    assert "/api/release_stale" not in src, \
        "启动路径不应内联 release_stale 请求，请改用 _release_stale()"

    src_run = inspect.getsource(pw.run_once)
    assert "/api/release_stale" not in src_run, \
        "轮询路径同样应走 _release_stale()，避免两份请求体再次分叉"


def test_release_stale_returns_count_and_skips_on_bad_outbox(tmp_path, monkeypatch):
    """_release_stale：正常时返回协调端计数；outbox 坏掉时直接返回 0 且不发请求。"""
    calls = []

    class FakeResp:
        def json(self):
            return {"ok": True, "released": 3}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            calls.append((url, json))
            return FakeResp()

    monkeypatch.setattr(pw.httpx, "Client", FakeClient)

    good = tmp_path / "out.jsonl"
    good.write_text(json.dumps({"task_id": 1, "platform": "kuaishou"}) + "\n",
                    encoding="utf-8")
    monkeypatch.setattr(pw, "PENDING_REPORTS_PATH", good)
    assert pw._release_stale("http://x", Settings(), 30) == 3
    assert calls[-1][1]["exclude"] == ["1:kuaishou"]

    bad = tmp_path / "broken.jsonl"
    bad.mkdir()
    monkeypatch.setattr(pw, "PENDING_REPORTS_PATH", bad)
    before = len(calls)
    assert pw._release_stale("http://x", Settings(), 30) == 0
    assert len(calls) == before, "outbox 不可读时不能真的发出回收请求"

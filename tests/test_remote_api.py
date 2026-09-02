"""Remote API + publish_worker 集成测试：临时 DB 起真 HTTP 服务，走完整认领-下载-回报流程。"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import httpx
import pytest

from bot.config import Settings
from bot.db import Database, JobState, State

# ---------- fixtures ----------

def _settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.remote_api_token = "test-token-123"
    s.remote_api_bind = "127.0.0.1"
    s.remote_api_port = 0  # OS 分配临时端口，避免并行冲突
    s.ai_api_key = ""
    return s


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    """起真 RemoteApi 服务：临时 DB + 一个 READY 任务（含 final/cover 假文件）。"""
    import bot.config as cfg_mod
    import bot.db as db_mod
    import bot.remote_api as ra

    tmp_db = tmp_path / "bot.db"
    monkeypatch.setattr(cfg_mod, "DB_PATH", tmp_db)
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_db)  # db.py 顶层 import 已固化，需双补丁
    monkeypatch.setattr(cfg_mod, "FINAL_DIR", tmp_path / "media" / "final")
    monkeypatch.setattr(cfg_mod, "WORK_DIR", tmp_path / "media" / "work")
    cfg_mod.FINAL_DIR.mkdir(parents=True, exist_ok=True)
    cfg_mod.WORK_DIR.mkdir(parents=True, exist_ok=True)
    db = Database(tmp_db)  # 显式传路径（默认参数在 import 时已绑定真实库路径）

    final = cfg_mod.FINAL_DIR / "abc_final.mp4"
    final.write_bytes(b"FAKE-VIDEO-CONTENT" * 1000)
    cover = cfg_mod.WORK_DIR / "abc_cover.jpg"
    cover.write_bytes(b"FAKE-JPEG" * 100)

    from bot.source.downloader import VideoMeta
    meta = VideoMeta(source_url="https://x/y", platform="youtube", video_id="abc",
                     shortcode="youtube_abc", username="tester", title="原始标题",
                     caption="原始描述", thumbnail_url="", duration=60.0,
                     permalink="https://x/y")
    tid = db.insert_video(meta)
    assert tid is not None, "任务入库失败（DB_PATH 补丁未生效）"
    db.update(tid, title="测试视频", description="描述", tags="a b", category="搞笑",
              state=State.READY, final_path=str(final), cover_path=str(cover),
              target_platforms="kuaishou")
    db.create_publish_jobs(tid, ["kuaishou"])

    s = _settings(tmp_path)
    api = ra.RemoteApi(s, db, gate_fn=lambda p: None)
    t = threading.Thread(target=api.serve_forever, args=("127.0.0.1", 0), daemon=True)
    t.start()
    time.sleep(0.3)
    port = api.server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    yield {"base": base, "db": db, "s": s, "task_id": tid,
           "final": final, "cover": cover}
    api.server.shutdown()


def _h(s):
    return {"Authorization": f"Bearer {s.remote_api_token}"}


# ---------- 鉴权 ----------

def test_health_no_auth(api_env):
    r = httpx.get(f"{api_env['base']}/api/health", timeout=5)
    assert r.status_code == 200 and r.json()["ok"] is True


def test_auth_required(api_env):
    r = httpx.get(f"{api_env['base']}/api/pending", timeout=5)
    assert r.status_code == 401
    r2 = httpx.get(f"{api_env['base']}/api/pending",
                   headers={"Authorization": "Bearer wrong"}, timeout=5)
    assert r2.status_code == 401


def test_auth_ban_after_failures(api_env):
    base, s = api_env["base"], api_env["s"]
    for _ in range(5):
        httpx.get(f"{base}/api/pending", headers={"Authorization": "Bearer bad"}, timeout=5)
    r = httpx.get(f"{base}/api/pending", headers=_h(s), timeout=5)
    assert r.status_code == 401  # 正确 token 也被封（同 IP）


# ---------- pending ----------

def test_pending_lists_ready_task(api_env):
    r = httpx.get(f"{api_env['base']}/api/pending", headers=_h(api_env["s"]), timeout=5)
    data = r.json()
    assert len(data["tasks"]) == 1
    t = data["tasks"][0]
    assert t["shortcode"] == "youtube_abc"
    assert t["title"] == "测试视频"
    assert [p["platform"] for p in t["platforms"]] == ["kuaishou"]
    assert t["final_file"]["size"] > 0
    assert t["final_file"]["sha256"] == hashlib.sha256(api_env["final"].read_bytes()).hexdigest()


# ---------- file 下载 ----------

def test_file_download_with_path_whitelist(api_env):
    s = api_env["s"]
    final = api_env["final"]
    r = httpx.get(f"{api_env['base']}/api/file", params={"path": str(final)},
                  headers=_h(s), timeout=10)
    assert r.status_code == 200 and r.content == final.read_bytes()
    # 白名单外路径拒绝
    r2 = httpx.get(f"{api_env['base']}/api/file", params={"path": "/etc/passwd"},
                   headers=_h(s), timeout=5)
    assert r2.status_code == 403


# ---------- claim / report 全流程 ----------

def test_claim_and_report_flow(api_env):
    base, s, db, tid = api_env["base"], api_env["s"], api_env["db"], api_env["task_id"]

    # claim：PENDING → PUBLISHING
    r = httpx.post(f"{base}/api/claim", headers=_h(s),
                   json={"task_id": tid, "platform": "kuaishou"}, timeout=5)
    data = r.json()
    assert data["ok"] is True
    assert data["task"]["shortcode"] == "youtube_abc"
    assert data["task"]["final_file"]["name"].endswith(".mp4")

    # 二次 claim 失败（防重复发布）
    r2 = httpx.post(f"{base}/api/claim", headers=_h(s),
                    json={"task_id": tid, "platform": "kuaishou"}, timeout=5)
    assert r2.json()["ok"] is False

    # report 成功 → job PUBLISHED + task PUBLISHED
    r3 = httpx.post(f"{base}/api/report", headers=_h(s),
                    json={"task_id": tid, "platform": "kuaishou", "ok": True,
                          "url": "https://www.kuaishou.com/new-video"}, timeout=5)
    assert r3.json()["ok"] is True
    jobs = db.jobs(tid)
    assert jobs[0]["state"] == JobState.PUBLISHED
    assert jobs[0]["url"] == "https://www.kuaishou.com/new-video"
    assert db.get(tid)["state"] == State.PUBLISHED


def test_report_login_expired_skips_job(api_env):
    base, s, db, tid = api_env["base"], api_env["s"], api_env["db"], api_env["task_id"]
    httpx.post(f"{base}/api/claim", headers=_h(s),
               json={"task_id": tid, "platform": "kuaishou"}, timeout=5)
    httpx.post(f"{base}/api/report", headers=_h(s),
               json={"task_id": tid, "platform": "kuaishou", "ok": False,
                     "error": "登录态失效", "login_expired": True}, timeout=5)
    jobs = db.jobs(tid)
    assert jobs[0]["state"] == JobState.SKIPPED
    assert db.get(tid)["state"] == State.PUBLISHED  # 全部终态 → 任务也收尾


def test_report_failure_back_to_pending(api_env):
    base, s, db, tid = api_env["base"], api_env["s"], api_env["db"], api_env["task_id"]
    httpx.post(f"{base}/api/claim", headers=_h(s),
               json={"task_id": tid, "platform": "kuaishou"}, timeout=5)
    httpx.post(f"{base}/api/report", headers=_h(s),
               json={"task_id": tid, "platform": "kuaishou", "ok": False,
                     "error": "页面超时"}, timeout=5)
    jobs = db.jobs(tid)
    assert jobs[0]["state"] == JobState.PENDING  # 回到待发布，等重试
    assert jobs[0]["retries"] == 1


def test_report_never_published_task_stays_subtitled(api_env):
    """失败回报后任务不应被标 PUBLISHED（还有 PENDING job）。"""
    base, s, db, tid = api_env["base"], api_env["s"], api_env["db"], api_env["task_id"]
    httpx.post(f"{base}/api/claim", headers=_h(s),
               json={"task_id": tid, "platform": "kuaishou"}, timeout=5)
    httpx.post(f"{base}/api/report", headers=_h(s),
               json={"task_id": tid, "platform": "kuaishou", "ok": False,
                     "error": "网络错误"}, timeout=5)
    assert db.get(tid)["state"] == State.READY

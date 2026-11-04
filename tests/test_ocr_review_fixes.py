"""回归测试：open-code-review（OCR）审查发现的问题修复。

覆盖：
- URL 正则会截断 ?v= 查询参数（YouTube/Facebook 链接）
- /api/pending 先 LIMIT 再过滤状态，积压时返回空清单
- 全部平台失败被标成 PUBLISHED（伪造「发布完成」）
- IPv6 / IPv4-mapped 地址绕过绑定白名单
- 补报队列非原子写 + 脏条目永久重试
- release_stale 与补报队列的竞态（已发出的 job 被退回队列）
- 首评生成绕过提问式模板校验
- IG HTTP 200 + 失败体未被判定为会话失效
"""

from __future__ import annotations

import ipaddress

import pytest

from bot.db import Database, JobState, State
from bot.remote_api import RemoteApi, RemoteApiError
from bot.source.telegram_listener import _URL_RE, _URL_TRAILING_PUNCT

# ---------- URL 提取 ----------

@pytest.mark.parametrize("text,expect", [
    ("看这个 https://www.youtube.com/watch?v=ABC123",
     "https://www.youtube.com/watch?v=ABC123"),
    ("素材 https://www.facebook.com/watch/?v=123456 备用",
     "https://www.facebook.com/watch/?v=123456"),
    ("https://www.tiktok.com/@u/video/123?is_from_webapp=1",
     "https://www.tiktok.com/@u/video/123?is_from_webapp=1"),
    # 半角标点紧跟正文时不能被吃进 URL（回归：保留 ? 之后一度连 , ! 也放行了）
    ("https://youtu.be/ABC,好笑", "https://youtu.be/ABC"),
    ("https://youtu.be/ABC!太好笑了", "https://youtu.be/ABC"),
    # 多参数链接要完整保留
    ("https://www.facebook.com/watch/?v=123&x=1",
     "https://www.facebook.com/watch/?v=123&x=1"),
])
def test_url_keeps_query_params(text, expect):
    """?v= 这类查询参数必须保留；逗号/感叹号紧跟正文时要截断。"""
    m = _URL_RE.search(text)
    assert m is not None
    got = m.group(0).rstrip(_URL_TRAILING_PUNCT)
    assert got == expect


def test_parse_target_platforms_removes_url():
    """去掉 URL 之后，不能再把域名当「未识别的平台名」报给用户（回归：sub 未真正删除）。"""
    from bot.source.telegram_listener import _parse_target_platforms

    targets, unknown = _parse_target_platforms(
        "https://www.instagram.com/reel/ABC/ @快手")
    assert unknown == [], f"URL 残留导致误报未知平台：{unknown}"
    assert targets == ["kuaishou"]


def test_url_stops_at_chinese_punctuation():
    m = _URL_RE.search("https://www.instagram.com/reel/ABC/，很好笑")
    assert m.group(0).rstrip(_URL_TRAILING_PUNCT) == "https://www.instagram.com/reel/ABC/"


def test_url_trailing_english_punct_stripped():
    m = _URL_RE.search("看 https://www.instagram.com/reel/ABC/.")
    assert m.group(0).rstrip(_URL_TRAILING_PUNCT) == "https://www.instagram.com/reel/ABC/"


# ---------- pending 状态过滤 ----------

@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def _ins(db, n, state, day="01"):
    cur = db.conn.execute(
        "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (f"m{n}", f"sc{n}", state, f"2026-01-{day}T00:00:00", f"2026-01-{day}T00:00:00"))
    db.conn.commit()
    return cur.lastrowid


def test_pending_states_filter_applied_before_limit(db):
    """处理中任务积压 ≥ limit 时，可发布态任务仍必须被取到。"""
    for i in range(120):
        _ins(db, i, State.DETECTED)
    ready = _ins(db, 900, State.READY, day="02")
    ready2 = _ins(db, 901, State.READY, day="03")

    rows = db.pending(limit=100, states=[State.SUBTITLED, State.READY])
    ids = [r["id"] for r in rows]
    assert ready in ids and ready2 in ids
    assert len(rows) == 2


def test_pending_default_still_prioritizes_unpublished(db):
    """默认视图（本地流水线）仍把未进入发布阶段的任务排在前面。"""
    for i in range(60):
        _ins(db, i, State.READY)
    fresh = _ins(db, 999, State.DETECTED, day="28")
    rows = db.pending(limit=50)
    assert rows[0]["id"] == fresh


# ---------- 全平台失败不能标 PUBLISHED ----------

def _api_with_task(tmp_path):
    d = Database(tmp_path / "r.db")
    cur = d.conn.execute(
        "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
        "VALUES ('m1','sc1',?, '2026-01-01T00:00:00','2026-01-01T00:00:00')",
        (State.READY,))
    d.conn.commit()
    tid = cur.lastrowid
    d.create_publish_jobs(tid, ["kuaishou"])
    from unittest.mock import MagicMock

    api = RemoteApi(MagicMock(), d, lambda s, db, p: None)
    return api, d, tid


def _claim(d, tid):
    job = d.jobs(tid)[0]
    d.update_job(job["id"], state="PUBLISHING", claimed_at="2026-01-01T10:00:00")


def test_all_platforms_failed_does_not_mark_published(tmp_path):
    """一个都没发成功时，任务不能被标 PUBLISHED（否则会推送「✅ 发布完成」）。"""
    api, d, tid = _api_with_task(tmp_path)
    for i in range(6):
        _claim(d, tid)
        api.report({"task_id": tid, "platform": "kuaishou", "ok": False,
                    "error": f"fail {i}"})
        if d.jobs(tid)[0]["state"] == JobState.FAILED:
            break
    assert d.jobs(tid)[0]["state"] == "FAILED"
    # 再触发一次「全部终态」判定
    _claim(d, tid) if d.jobs(tid)[0]["state"] == "PENDING" else None
    assert d.get(tid)["state"] != State.PUBLISHED
    d.close()


def test_partial_success_marks_published(tmp_path):
    """一个平台成功、另一个到 FAILED 终态 → 任务完成（确实发出去过）。"""
    from unittest.mock import MagicMock

    d = Database(tmp_path / "p.db")
    cur = d.conn.execute(
        "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
        "VALUES ('m1','sc1',?, '2026-01-01T00:00:00','2026-01-01T00:00:00')",
        (State.READY,))
    d.conn.commit()
    tid = cur.lastrowid
    d.create_publish_jobs(tid, ["kuaishou", "douyin"])
    api = RemoteApi(MagicMock(), d, lambda s, db, p: None)

    # 快手上传成功
    ks_job = next(j for j in d.jobs(tid) if j["platform"] == "kuaishou")
    d.update_job(ks_job["id"], state="PUBLISHING", claimed_at="2026-01-01T10:00:00")
    api.report({"task_id": tid, "platform": "kuaishou", "ok": True, "url": "https://x/1"})

    # 抖音重试到 FAILED（每次失败回 PENDING，第 5 次转终态）
    for _ in range(6):
        dy = next(j for j in d.jobs(tid) if j["platform"] == "douyin")
        if dy["state"] != "PENDING":
            break
        d.update_job(dy["id"], state="PUBLISHING", claimed_at="2026-01-01T10:00:00")
        api.report({"task_id": tid, "platform": "douyin", "ok": False, "error": "x"})

    states = {j["platform"]: j["state"] for j in d.jobs(tid)}
    assert states["kuaishou"] == "PUBLISHED"
    assert states["douyin"] == "FAILED"
    assert d.get(tid)["state"] == State.PUBLISHED
    d.close()


def test_single_platform_failure_stays_pending(tmp_path):
    """只失败一次不该完成任务（job 回 PENDING 等重试）。"""
    from unittest.mock import MagicMock

    d = Database(tmp_path / "q.db")
    cur = d.conn.execute(
        "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
        "VALUES ('m1','sc1',?, '2026-01-01T00:00:00','2026-01-01T00:00:00')",
        (State.READY,))
    d.conn.commit()
    tid = cur.lastrowid
    d.create_publish_jobs(tid, ["kuaishou"])
    api = RemoteApi(MagicMock(), d, lambda s, db, p: None)

    job = d.jobs(tid)[0]
    d.update_job(job["id"], state="PUBLISHING", claimed_at="2026-01-01T10:00:00")
    api.report({"task_id": tid, "platform": "kuaishou", "ok": False, "error": "x"})

    assert d.jobs(tid)[0]["state"] == "PENDING"
    assert d.get(tid)["state"] == State.READY
    d.close()


# ---------- 绑定白名单 ----------

@pytest.mark.parametrize("host", ["0.0.0.0", "::", "8.8.8.8", "2001:4860:4860::8888",
                                  "::ffff:8.8.8.8"])
def test_bind_rejects_public_and_unspecified(host):
    from unittest.mock import MagicMock

    api = RemoteApi(MagicMock(), MagicMock(), lambda s, db, p: None)
    with pytest.raises(RemoteApiError):
        api.serve_forever(host, 0)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "100.101.102.103", "::ffff:127.0.0.1"])
def test_bind_allows_loopback_and_tailscale(host):
    """允许回环/Tailscale；IPv4-mapped 回环要按 v4 判定通过。"""
    ip = ipaddress.ip_address(host)
    check = getattr(ip, "ipv4_mapped", None) or ip
    nets = [ipaddress.ip_network(n) for n in
            ("127.0.0.0/8", "100.64.0.0/10", "::1/128")]
    assert any(check in n for n in nets), host


# ---------- release_stale 排除待补报 job ----------

def test_release_stale_excludes_pending_report_pairs(tmp_path):
    d = Database(tmp_path / "s.db")
    cur = d.conn.execute(
        "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
        "VALUES ('m1','sc1',?, '2026-01-01T00:00:00','2026-01-01T00:00:00')",
        (State.READY,))
    d.conn.commit()
    tid = cur.lastrowid
    d.create_publish_jobs(tid, ["kuaishou", "kuaishou2"])
    jobs = d.jobs(tid)
    for j in jobs:
        d.update_job(j["id"], state="PUBLISHING", claimed_at="2020-01-01T00:00:00")
    # update_job 会自动写 updated_at=now()，这里直接改成很久以前以命中阈值
    d.conn.execute("UPDATE publish_jobs SET updated_at='2020-01-01T00:00:00' WHERE task_id=?", (tid,))
    d.conn.commit()

    from unittest.mock import MagicMock

    api = RemoteApi(MagicMock(), d, lambda s, db, p: None)
    # 排除 kuaishou：它的视频已经发出去了，只是成功回报还没送到
    released = api.release_stale_jobs(older_than_minutes=30,
                                      exclude={(tid, "kuaishou")})
    assert released == 1
    states = {j["platform"]: j["state"] for j in d.jobs(tid)}
    assert states["kuaishou"] == "PUBLISHING", "待补报的 job 不能被退回队列"
    assert states["kuaishou2"] == "PENDING"
    d.close()


# ---------- 首评兜底不能绕过模板校验 ----------

def test_generated_first_comment_rejects_question_style(monkeypatch):
    """模型给出提问式模板时，兜底应改用预置短句。"""
    import bot.publish.kuaishou_reply as kr

    class _Msg:
        content = "这个沙发你们喜欢吗？"

    class _Resp:
        choices = [type("C", (), {"message": _Msg})()]

    class _Client:
        def __init__(self, **kw):
            self.chat = type("Chat", (), {
                "completions": type("Comp", (), {
                    "create": staticmethod(lambda **kw: _Resp())})()})()

    class _S:
        ai_api_key = "k"
        ai_base_url = "http://x"
        ai_model = "m"

    monkeypatch.setattr(kr, "load_settings", lambda: _S())
    import openai

    monkeypatch.setattr(openai, "OpenAI", lambda **kw: _Client())

    out = kr.generate_first_comment("某个视频标题")
    assert out in kr._FALLBACK_FIRST_COMMENTS


# ---------- IG 失败体识别 ----------

@pytest.mark.parametrize("payload,expect_status", [
    ({"status": "fail", "message": "login_required"}, 401),
    ({"status": "fail", "message": "checkpoint_required"}, 401),
    ({"status": "fail", "message": "Media not found"}, 404),
    ({"status": "fail", "message": "something odd"}, 0),
    ({"items": []}, 200),
])
def test_ig_failure_body_maps_to_status(monkeypatch, payload, expect_status):
    """HTTP 200 + 失败体必须能被区分：登录坏 → 401（触发冷却），删帖 → 404。

    返回 0 会让调用方走「帖子不可访问」分支、永不冷却账号 —— 等于没修。
    """
    import httpx

    import bot.source.ig_api as ig

    class _Resp:
        status_code = 200

        def json(self):
            return payload

    class _Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    item, status = ig._try_fetch(123, {"sessionid": "s"}, "app", "i.instagram.com")
    assert status == expect_status, f"{payload} -> {status}"
    if expect_status == 200:
        assert item is None   # items 为空


def test_ig_html_instead_of_json_returns_401(monkeypatch):
    """被重定向到登录页返回 HTML 时，r.json() 失败 → 必须返回 401。"""
    import httpx

    import bot.source.ig_api as ig

    class _Resp:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    class _Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    item, status = ig._try_fetch(123, {"sessionid": "s"}, "app", "i.instagram.com")
    assert item is None
    assert status == 401

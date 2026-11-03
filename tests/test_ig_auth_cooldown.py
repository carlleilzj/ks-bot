"""回归测试：IG 会话失效必须冷却账号（2026-11-04 深度审计修复）。

历史问题：`_try_fetch` 对 401/403/429 一律返回 None，`extract()` 统一抛
「帖子不可访问」，账号永不冷却 → 坏账号因 use_count 最低被每轮优先选中，
形成稳定失败循环，运维看到的现象却是「帖子被删了」。
"""

from __future__ import annotations

import pytest

import bot.source.ig_api as ig_api


@pytest.fixture()
def fake_env(monkeypatch):
    blocked: list[tuple[str, str]] = []

    monkeypatch.setattr(ig_api.ig_pool, "pick_cookiefile", lambda: "/tmp/fake_cookies.txt")
    monkeypatch.setattr(ig_api.ig_pool, "account_of", lambda cf: "acct1")
    monkeypatch.setattr(ig_api, "_read_cookie", lambda p: {"sessionid": "S"})
    monkeypatch.setattr(ig_api, "shortcode_to_pk", lambda sc: 123)
    monkeypatch.setattr(
        ig_api.ig_pool, "report_blocked",
        lambda u, r, **kw: blocked.append((u, r)),
    )
    monkeypatch.setattr(ig_api.ig_pool, "report_ok", lambda u: None)
    return blocked


@pytest.mark.parametrize("status", [401, 403, 429])
def test_auth_failures_cooldown_account(fake_env, monkeypatch, status):
    monkeypatch.setattr(ig_api, "_try_fetch", lambda pk, c, a, h: (None, status))
    with pytest.raises(ValueError) as ei:
        ig_api.extract("https://www.instagram.com/reel/ABC123/")
    assert "冷却" in str(ei.value) or "限流" in str(ei.value)
    assert fake_env and fake_env[0][0] == "acct1"
    assert str(status) in fake_env[0][1]


def test_404_does_not_cooldown_account(fake_env, monkeypatch):
    """帖子真的没了（404）不该冤枉账号。"""
    monkeypatch.setattr(ig_api, "_try_fetch", lambda pk, c, a, h: (None, 404))
    with pytest.raises(ValueError) as ei:
        ig_api.extract("https://www.instagram.com/reel/ABC123/")
    assert "不可访问" in str(ei.value)
    assert fake_env == []


def test_success_calls_report_ok(fake_env, monkeypatch):
    ok_calls = []
    monkeypatch.setattr(ig_api.ig_pool, "report_ok", lambda u: ok_calls.append(u))
    monkeypatch.setattr(
        ig_api, "_try_fetch",
        lambda pk, c, a, h: ({"user": {"username": "creator"},
                              "video_versions": [{"url": "http://v.mp4", "width": 720}],
                              "media_type": 2, "video_duration": 10.0,
                              "caption": {"text": "hello"}}, 200),
    )
    meta = ig_api.extract("https://www.instagram.com/reel/ABC123/")
    assert meta.username == "creator"
    assert ok_calls == ["acct1"]

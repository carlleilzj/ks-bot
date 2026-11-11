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


def test_read_cookie_filters_instagram_domain_only(tmp_path):
    """全量导出文件中含抖音 sessionid 时，_read_cookie 绝不能被非 IG 域污染。"""
    cf = tmp_path / "mixed.txt"
    cf.write_text(
        "# Netscape HTTP Cookie File\n"
        ".douyin.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tDOUYIN_SID\n"
        "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tIG_SID\n"
        ".instagram.com\tTRUE\t/\tTRUE\t9999999999\tcsrftoken\tIG_CSRF\n",
        encoding="utf-8",
    )
    cookies = ig_api._read_cookie(cf)
    assert cookies.get("sessionid") == "IG_SID"
    assert cookies.get("csrftoken") == "IG_CSRF"


def test_ig_discover_profile_prefers_anonymous_and_does_not_require_cookie(tmp_path, monkeypatch):
    """白名单主页抓取优先匿名，即使完全没有 cookie 也能调到抓取流程。"""
    from bot.source import ig_discover as igd

    calls = []

    def fake_scrape(path, limit, cookiefile, cookie_dir, label="", pages=1,
                    allow_anonymous=False, prefer_anonymous=False):
        calls.append((path, allow_anonymous, prefer_anonymous))
        return [{"url": "https://www.instagram.com/reel/XYZ/", "code": "XYZ"}]

    monkeypatch.setattr(igd, "_scrape_links", fake_scrape)
    out = igd.discover_instagram_profile("kataicinema", cookie_dir=tmp_path)
    assert len(out) == 1
    assert calls[0][1] is True
    assert calls[0][2] is True


def test_ig_discover_tag_skips_cleanly_without_cookie(tmp_path):
    """标签探索无有效 sessionid 时不启动浏览器、直接跳过返回空。"""
    from bot.source import ig_discover as igd

    assert igd.discover_instagram_tag("3danimation", cookie_dir=tmp_path) == []

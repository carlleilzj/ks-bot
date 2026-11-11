"""测试 downloader 的多平台专用 Cookie 路由。"""
from pathlib import Path
from bot.source import downloader as dl


def test_pick_cookies_routes_correctly(tmp_path, monkeypatch):
    cookie_dir = tmp_path / "cookies.d"
    cookie_dir.mkdir(parents=True)
    
    (cookie_dir / "douyin.txt").write_text("dummy", encoding="utf-8")
    (cookie_dir / "xhs.txt").write_text("dummy", encoding="utf-8")
    (cookie_dir / "kuaishou.txt").write_text("dummy", encoding="utf-8")
    (cookie_dir / "toutiao.txt").write_text("dummy", encoding="utf-8")
    
    monkeypatch.setattr(dl, "_COOKIE_DIR", cookie_dir)
    
    p, stem = dl._pick_cookies("https://www.douyin.com/video/123")
    assert p == cookie_dir / "douyin.txt"
    assert stem == "douyin"
    
    p, stem = dl._pick_cookies("https://v.douyin.com/abc/")
    # v.douyin.com contains douyin.com
    assert p == cookie_dir / "douyin.txt"
    
    p, stem = dl._pick_cookies("https://www.xiaohongshu.com/explore/123")
    assert p == cookie_dir / "xhs.txt"
    assert stem == "xhs"

    p, stem = dl._pick_cookies("https://xhslink.com/abc")
    assert p == cookie_dir / "xhs.txt"

    p, stem = dl._pick_cookies("https://www.kuaishou.com/short-video/123")
    assert p == cookie_dir / "kuaishou.txt"

    p, stem = dl._pick_cookies("https://www.toutiao.com/video/123")
    assert p == cookie_dir / "toutiao.txt"

    (cookie_dir / "bilibili.txt").write_text("dummy", encoding="utf-8")
    p, stem = dl._pick_cookies("https://www.bilibili.com/video/BV123")
    assert p == cookie_dir / "bilibili.txt"
    assert stem == "bilibili"

    p, stem = dl._pick_cookies("https://b23.tv/abc")
    assert p == cookie_dir / "bilibili.txt"
    assert stem == "bilibili"


def test_pick_cookies_instagram_returns_none_without_sessionid(tmp_path, monkeypatch):
    """IG 无含 sessionid 的可用 cookie 时，必须返回 (None, '') 走纯净匿名 yt-dlp，不能回落到遗留 cookies.txt。"""
    from bot.source import ig_pool

    legacy = tmp_path / "legacy_cookies.txt"
    legacy.write_text("# legacy stale cookies", encoding="utf-8")
    monkeypatch.setattr(dl, "_COOKIE_FILE", legacy)
    monkeypatch.setattr(ig_pool, "pick_cookiefile", lambda: None)

    p, stem = dl._pick_cookies("https://www.instagram.com/reel/DdHFxhYsKgz/")
    assert p is None
    assert stem == ""

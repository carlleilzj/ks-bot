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

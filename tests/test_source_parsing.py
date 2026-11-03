"""回归测试：采集链路的解析边界（2026-11-04 深度审计修复）。

- TG 投链中文标点不能吃进 URL
- IG 分享链的 igsh/igshid 追踪参数必须剥干净
- `#HttpOnly_` 前缀的 cookie 行要能正常解析
"""

from __future__ import annotations

from bot.source.downloader import parse_url
from bot.source.telegram_listener import _URL_RE


def test_url_regex_stops_at_chinese_punctuation():
    text = "看这个 https://www.instagram.com/reel/AbCdEf12345/，很好笑"
    m = _URL_RE.search(text)
    assert m is not None
    url = m.group(0)
    assert url == "https://www.instagram.com/reel/AbCdEf12345/"
    assert "，" not in url


def test_url_regex_handles_full_width_paren():
    text = "素材（https://www.instagram.com/reel/XyZ987/）备用"
    url = _URL_RE.search(text).group(0)
    assert url.endswith("/XyZ987/")


def test_parse_url_strips_igsh_variants():
    base = parse_url("https://www.instagram.com/reel/ABC123/")
    for extra in ("?igsh=AAAA", "?igshid=BBBB", "?utm_source=ccc&igsh=DDDD"):
        got = parse_url(f"https://www.instagram.com/reel/ABC123/{extra}")
        assert got == base, f"{extra} 未被剥离（去重会失效）"


def test_cookie_httponly_prefix_is_parsed(tmp_path):
    """Chrome 导出的 #HttpOnly_ 行必须能被识别出 sessionid。"""
    cf = tmp_path / "cookies.txt"
    cf.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tSECRETVAL\n",
        encoding="utf-8",
    )
    from bot.source.ig_pool import _has_sessionid

    assert _has_sessionid(cf) is True

    from bot.source.ig_api import _read_cookie

    cookies = _read_cookie(cf)
    assert cookies.get("sessionid") == "SECRETVAL"


def test_cookie_plain_comment_still_skipped(tmp_path):
    cf = tmp_path / "cookies.txt"
    cf.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    from bot.source.ig_pool import _has_sessionid

    assert _has_sessionid(cf) is False

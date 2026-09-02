"""无浏览器单测：URL 规范化、发布闸门、文案校验、发布浏览器代理环境。"""

from __future__ import annotations

from datetime import datetime

from bot.ai.copywriter import PLATFORM_PROFILES, _validate
from bot.config import Settings
from bot.db import Database, JobState
from bot.main import publish_gate
from bot.publish.base import PROXY_ENV_KEYS, chromium_launch_env, chromium_launch_kwargs
from bot.publish.kuaishou import pick_spark_title
from bot.publish.weixin import _sanitize_weixin_url
from bot.source.downloader import parse_url


def test_parse_url_strips_tracking():
    raw = "https://www.instagram.com/reel/ABC123/?igsi=test&utm_source=x&fbclid=1#frag"
    got = parse_url(raw)
    assert "igsi" not in got
    assert "utm_source" not in got
    assert "fbclid" not in got
    assert "#frag" not in got
    assert "ABC123" in got


def test_copy_validate_truncates_title_and_tags():
    profile = PLATFORM_PROFILES["xhs"]
    obj = {
        "title": "这是一个远远超过二十个字的小红书标题必须被截断才行",
        "description": "desc",
        "tags": ["#美食", "超长标签超过十二个字会被切", "ok", "extra1", "extra2", "extra3"],
        "category": "应被清空",
    }
    out = _validate(obj, ["搞笑"], profile)
    assert len(out["title"]) <= profile.max_title_len
    assert out["category"] == ""
    assert all(not t.startswith("#") for t in out["tags"])
    assert len(out["tags"]) <= profile.max_tags


def test_copy_validate_category_fuzzy():
    profile = PLATFORM_PROFILES["kuaishou"]
    out = _validate(
        {"title": "标题", "description": "", "tags": ["a"], "category": "美食教程"},
        ["搞笑", "美食"],
        profile,
    )
    assert out["category"] == "美食"


def test_chromium_launch_env_strips_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7890")
    env = chromium_launch_env()
    for key in PROXY_ENV_KEYS:
        assert key not in env


def test_chromium_kwargs_direct_by_default(monkeypatch):
    monkeypatch.delenv("PUBLISH_PROXY", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    kw = chromium_launch_kwargs(headless=True)
    assert "--no-proxy-server" in kw["args"]
    assert "proxy" not in kw
    for key in PROXY_ENV_KEYS:
        assert key not in kw["env"]


def test_chromium_kwargs_optional_proxy(monkeypatch):
    monkeypatch.setenv("PUBLISH_PROXY", "http://127.0.0.1:7890")
    kw = chromium_launch_kwargs(headless=True)
    assert kw["proxy"] == {"server": "http://127.0.0.1:7890"}
    assert any(a.startswith("--proxy-bypass-list=") for a in kw["args"])
    assert "--no-proxy-server" not in kw["args"]


def test_pick_spark_title_rotate_and_prefer():
    titles = ["狐缘山间", "扫墓丫头，替祖宗横扫天下", "新赛季你的本命英雄是？"]
    a, c1 = pick_spark_title(titles, cursor=0)
    b, c2 = pick_spark_title(titles, cursor=c1)
    c, c3 = pick_spark_title(titles, cursor=c2)
    d, _ = pick_spark_title(titles, cursor=c3)
    assert [a, b, c, d] == titles + titles[:1]
    preferred, same = pick_spark_title(titles, prefer="本命英雄", cursor=9)
    assert preferred == "新赛季你的本命英雄是？"
    assert same == 9
    empty, cur = pick_spark_title([], prefer="x", cursor=3)
    assert empty == "" and cur == 3


def test_weixin_agreement_url_rejected():
    bad = "https://weixin.qq.com/cgi-bin/readtemplate?t=weixin_agreement&s=video"
    assert _sanitize_weixin_url(bad) is None
    assert _sanitize_weixin_url("https://channels.weixin.qq.com/platform/post/list") is not None


def test_publish_gate_window_and_limit(tmp_path):
    db = Database(tmp_path / "t.db")
    s = Settings()
    s.publish.window = ("10:00", "22:00")
    s.publish.daily_limit = 1
    s.publish.min_gap_hours = 2.0

    now = datetime.now()
    minutes = now.hour * 60 + now.minute
    in_window = 10 * 60 <= minutes < 22 * 60
    reason = publish_gate(s, db, "kuaishou")
    if not in_window:
        assert reason and "不在发布窗口" in reason
        db.close()
        return

    assert reason is None
    db.conn.execute(
        """INSERT INTO publish_jobs
           (task_id, platform, state, published_at, created_at, updated_at)
           VALUES (1, 'kuaishou', ?, ?, ?, ?)""",
        (JobState.PUBLISHED, now.isoformat(timespec="seconds"),
         now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
    )
    db.conn.commit()
    reason = publish_gate(s, db, "kuaishou")
    assert reason is not None
    # 日上限或间隔，二者之一
    assert "上限" in reason or "间隔" in reason
    # 其他平台不受影响
    assert publish_gate(s, db, "douyin") is None
    db.close()

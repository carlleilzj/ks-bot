"""无浏览器单测：URL 规范化、发布闸门、文案校验、发布浏览器代理环境。"""

from __future__ import annotations

from datetime import datetime

from bot.ai.copywriter import PLATFORM_PROFILES, _check_cliche, _validate
from bot.publish.douyin import _strip_leading_title
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


# ---------------------------------------------------------------------------
# 标题固定前缀【有点视频】（2026-09-19 He 指定，替代旧三前缀体系）
# ---------------------------------------------------------------------------

def test_title_prefix_fixed_when_correct():
    profile = PLATFORM_PROFILES["kuaishou"]
    out = _validate({"title": "【有点视频】小家伙憋了半天气，下一秒全场最佳",
                     "description": "d", "tags": ["无声视频", "神反转"],
                     "category": ""}, [], profile)
    assert out["title"].startswith("【有点视频】")


def test_title_prefix_migrates_legacy_prefixes():
    """旧三前缀（无声治愈/无声小剧场/纯享放松）一律迁移到【有点视频】。"""
    profile = PLATFORM_PROFILES["kuaishou"]
    for legacy in ("【无声治愈】暴风雨里互相依偎的小家伙",
                   "【无声小剧场】两只小家伙抢玉米",
                   "【纯享放松】安静看完太解压"):
        out = _validate({"title": legacy, "description": "d",
                         "tags": ["无声视频"], "category": ""}, [], profile)
        assert out["title"].startswith("【有点视频】"), legacy


def test_title_prefix_migrates_bare_prefix():
    """模型漏写【】也要能迁移。"""
    profile = PLATFORM_PROFILES["kuaishou"]
    out = _validate({"title": "无声小剧场两只小家伙抢玉米，下一秒翻车",
                     "description": "d", "tags": ["无声视频"], "category": ""},
                    [], profile)
    assert out["title"].startswith("【有点视频】")


def test_title_prefix_added_when_missing():
    """完全没有前缀时自动补【有点视频】。"""
    profile = PLATFORM_PROFILES["kuaishou"]
    out = _validate({"title": "它守了这颗蛋整整一夜",
                     "description": "d", "tags": ["无声视频"], "category": ""},
                    [], profile)
    assert out["title"].startswith("【有点视频】")


def test_title_prefix_no_double_prefix():
    """已经是【有点视频】时绝不出现双重前缀。"""
    profile = PLATFORM_PROFILES["kuaishou"]
    out = _validate({"title": "【有点视频】正常标题",
                     "description": "d", "tags": ["无声视频"], "category": ""},
                    [], profile)
    assert out["title"].count("【有点视频】") == 1


def test_validate_strips_title_from_description():
    """简介抄标题时校验层先砍掉，避免抖音双框叠句。"""
    profile = PLATFORM_PROFILES["douyin"]
    title = "【有点视频】它自以为走位天衣无缝，直到下一秒啪唧摔出原形"
    out = _validate({"title": title, "description": title + "全程偷感拉满。",
                     "tags": ["无声视频"], "category": ""}, [], profile)
    assert not out["description"].startswith(title)
    assert "全程偷感拉满" in out["description"]


def test_strip_leading_title_exact():
    title = "【有点视频】它迈着六亲不认的步伐，结果下一秒帅不过三秒"
    desc = title + " 走出了最神气的姿势。"
    assert _strip_leading_title(title, desc) == "走出了最神气的姿势。"


def test_strip_leading_title_hook_only():
    title = "【有点视频】它迈着六亲不认的步伐"
    desc = "它迈着六亲不认的步伐，下一刻却当场破功。"
    assert _strip_leading_title(title, desc) == "下一刻却当场破功。"


def test_strip_leading_title_keeps_unique_desc():
    title = "【有点视频】走位翻车"
    desc = "全程无声却浑身是戏。"
    assert _strip_leading_title(title, desc) == desc


# ---------------------------------------------------------------------------
# 二创变换配置（抖音「原创性不足」）
# ---------------------------------------------------------------------------

def test_build_transform_defaults_off():
    from bot.config import _build_transform
    cfg = _build_transform(None)
    assert cfg.enabled is False


def test_build_transform_parses_ranges():
    from bot.config import _build_transform
    cfg = _build_transform({
        "enabled": True,
        "crop_pct": [0.02, 0.05],
        "tempo": [0.97, 1.03],
        "crf": [19, 24],
        "hflip_prob": 0.5,
    })
    assert cfg.enabled is True
    assert cfg.crop_pct == (0.02, 0.05)
    assert cfg.tempo == (0.97, 1.03)
    assert cfg.crf == (19.0, 24.0)
    assert cfg.hflip_prob == 0.5


def test_build_transform_clamps_hflip():
    from bot.config import _build_transform
    assert _build_transform({"hflip_prob": 5}).hflip_prob == 1.0
    assert _build_transform({"hflip_prob": -2}).hflip_prob == 0.0


def test_build_transform_bad_range_falls_back():
    """区间写错不能崩，退回默认值。"""
    from bot.config import TransformConfig, _build_transform
    cfg = _build_transform({"crop_pct": ["a", "b"]})
    assert cfg.crop_pct == TransformConfig().crop_pct


# ---------------------------------------------------------------------------
# 标题套话拦截（文案与内容牛头不对马嘴的对策）
# 实测基线：40 条已发标题里「下一秒」47%、「天衣无缝」25%、「自以为」22%
# ---------------------------------------------------------------------------

def test_cliche_catches_next_second():
    assert _check_cliche("【有点视频】本以为是个王者，下一秒直接翻车")


def test_cliche_catches_all_banned_words():
    for w in ("走位", "翻车", "滑跪", "脸刹", "天衣无缝", "王者",
              "帅不过", "下一秒", "自以为", "原地", "打滑", "起飞"):
        assert _check_cliche(f"【有点视频】它{w}了"), w


def test_cliche_catches_bad_opening():
    assert any("开头" in h for h in _check_cliche("【有点视频】它觉得自己藏得很好"))
    assert any("开头" in h for h in _check_cliche("【有点视频】它以为没人看见"))
    assert any("开头" in h for h in _check_cliche("【有点视频】本以为稳了"))


def test_cliche_passes_clean_title():
    """照实描述画面的标题必须放行。"""
    assert _check_cliche("【有点视频】蜥蜴和小蛇在丛林里赛跑，半路杀出个河马") == []
    assert _check_cliche("【有点视频】鬣狗滚成球冲下悬崖，青蛙看傻了") == []


def test_cliche_allows_bad_word_mid_sentence():
    """套话词在句中一样拦——它们是词，不是句式。"""
    hits = _check_cliche("【有点视频】这波走位我给满分")
    assert "走位" in hits


# ---------------------------------------------------------------------------
# 画面内容探针
# ---------------------------------------------------------------------------

def test_content_probe_caches_result(tmp_path):
    """探针结果要落盘缓存，重跑不重复调 vision。"""
    import json

    from bot.ai import content_probe

    sc = "test_sc"
    cache = tmp_path / f"{sc}_content.json"
    cache.write_text(json.dumps({"summary": "缓存命中", "subject": "猫"}),
                     encoding="utf-8")

    class _S:
        ai_base_url = ""
        ai_api_key = ""
        vision_api_key = ""
        vision_model = ""
        ai_model = ""

    # 缓存存在时直接返回，不应触碰 video 存在性检查
    data = content_probe.describe_video(tmp_path / "nope.mp4", _S(), tmp_path, sc)
    assert data["summary"] == "缓存命中"


def test_content_probe_missing_video_returns_empty(tmp_path):
    from bot.ai import content_probe

    class _S:
        ai_base_url = ""
        ai_api_key = ""
        vision_api_key = ""
        vision_model = ""
        ai_model = ""

    assert content_probe.describe_video(tmp_path / "nope.mp4", _S(), tmp_path, "sc2") == {}


def test_generate_copy_accepts_content_kwarg():
    """generate_copy 必须接受 content 参数（画面探针结果）。"""
    import inspect

    from bot.ai.copywriter import generate_copy
    sig = inspect.signature(generate_copy)
    assert "content" in sig.parameters


# ---------------------------------------------------------------------------
# 真人素材兜底判定（task 241 混入真人婴儿视频的教训）
# ---------------------------------------------------------------------------

def test_real_person_footage_detected():
    from bot.ai.content_probe import is_real_person_footage
    assert is_real_person_footage({"is_animation": False, "has_real_person": True})


def test_real_person_footage_animation_exempt():
    """动画里的角色不算真人。"""
    from bot.ai.content_probe import is_real_person_footage
    assert not is_real_person_footage({"is_animation": True, "has_real_person": False})


def test_real_person_footage_missing_fields_not_flagged():
    """字段缺失时不误杀——宁可漏判也不能把正常素材拦掉。"""
    from bot.ai.content_probe import is_real_person_footage
    assert not is_real_person_footage({})
    assert not is_real_person_footage(None)
    assert not is_real_person_footage({"summary": "小猫在跳舞"})
    assert not is_real_person_footage({"has_real_person": None, "is_animation": None})


def test_real_person_footage_animation_unknown_but_person_true():
    """没判定动画、但明确有真人 → 按真人处理。"""
    from bot.ai.content_probe import is_real_person_footage
    assert is_real_person_footage({"has_real_person": True})

"""无浏览器单测：URL 规范化、发布闸门、文案校验、发布浏览器代理环境。"""

from __future__ import annotations

from datetime import datetime

from bot.ai.copywriter import PLATFORM_PROFILES, _check_cliche, _validate
from bot.publish.douyin import _strip_leading_title
from bot.config import PlatformConfig, Settings
from bot.db import Database, JobState
from bot.main import publish_gate
from bot.publish.base import PROXY_ENV_KEYS, chromium_launch_env, chromium_launch_kwargs
from bot.publish.kuaishou import pick_spark_title
from bot.publish.weixin import _login_complete, _sanitize_weixin_url
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


# ---------------------------------------------------------------------------
# 话题组合：锚 + AI 内容话题 + 固定池补齐（2026-10-23）
# ---------------------------------------------------------------------------

def test_merge_tags_keeps_anchor_and_content_slots():
    from bot.ai.copywriter import merge_tags
    out = merge_tags(["萌宠日常", "动物搞笑", "沙雕瞬间"], 4)
    assert out[0] == "无声视频"
    assert "萌宠日常" in out and "动物搞笑" in out
    assert len(out) == 4
    # 固定池补齐：内容位占 2 个后还剩 1 个位给池子
    assert "搞笑日常" in out


def test_merge_tags_dedupes_same_family():
    """同家族近义词只留第一个，不堆叠。"""
    from bot.ai.copywriter import merge_tags
    out = merge_tags(["纯享放松", "视觉纯享", "治愈瞬间"], 4)
    assert "纯享放松" in out
    assert "视觉纯享" not in out


def test_merge_tags_falls_back_to_fixed_pool():
    """AI 没给内容话题时，回固定组合，条数补满。"""
    from bot.ai.copywriter import merge_tags
    out = merge_tags([], 4)
    assert out == ["无声视频", "搞笑日常", "沙雕视频", "搞笑动画"]


def test_merge_tags_custom_slots_and_pool():
    """kuaishou2：3 个内容位 + 指定池。"""
    from bot.ai.copywriter import merge_tags
    out = merge_tags(["居家好物", "四件套", "卧室改造", "多余"], 4,
                     content_slots=3,
                     fixed_pool=("好物分享", "变美日常", "穿搭分享"))
    assert out[0] == "无声视频"
    assert len(out) == 4
    # 3 个内容位都进来，池子不占位
    assert "居家好物" in out and "四件套" in out and "卧室改造" in out
    assert "多余" not in out


def test_merge_tags_no_base_for_kuaishou2():
    """B 号不锚「无声视频」（教程有人声），AI 话题 + 女性向池补齐。"""
    from bot.ai.copywriter import merge_tags
    out = merge_tags(["美妆教程", "新手化妆"], 4, base_tag="",
                     content_slots=3,
                     fixed_pool=("好物分享", "变美日常", "穿搭分享"))
    assert "无声视频" not in out
    assert out[:2] == ["美妆教程", "新手化妆"]
    assert out[2] == "好物分享"      # 池子补到满 4
    assert len(out) == 4


# ---------------------------------------------------------------------------
# 快手2号（矩阵号）：注册 / 登录态路径 / @别名
# ---------------------------------------------------------------------------

def test_kuaishou2_registered_with_own_state():
    from bot.publish import get_publisher, state_path_for
    pub = get_publisher("kuaishou2")
    assert pub.display_name == "快手2号"
    assert pub.state_path.name == "ks2_state.json"
    # A 号路径不受影响
    assert state_path_for("kuaishou").name == "ks_state.json"
    assert get_publisher("kuaishou").state_path.name == "ks_state.json"


def test_kuaishou2_spark_sidecars_isolated():
    """星火轮换/任务池文件按登录态派生，A 号沿用历史文件名。"""
    from bot.publish.kuaishou import _spark_sidecars
    rr_a, pool_a = _spark_sidecars("ks_state.json")
    assert rr_a.name == "ks_spark_rr.json"
    assert pool_a.name == "ks_spark_pool.json"
    rr_b, pool_b = _spark_sidecars("ks2_state.json")
    assert rr_b.name == "ks2_state_rr.json"
    assert pool_b.name == "ks2_state_pool.json"
    assert rr_a != rr_b and pool_a != pool_b


def test_parse_target_platforms_kuaishou2():
    """@快手2 别名解析，且与 @快手 不混淆。"""
    from bot.source.telegram_listener import _parse_target_platforms
    t, unknown = _parse_target_platforms("https://www.instagram.com/p/xx/ @快手2")
    assert t == ["kuaishou2"]
    t2, _ = _parse_target_platforms("https://www.instagram.com/p/xx/ @快手")
    assert t2 == ["kuaishou"]
    t3, _ = _parse_target_platforms("https://youtu.be/x @快手2 @快手")
    assert t3 == ["kuaishou2", "kuaishou"]


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
    """轮换按收益加权（2026-10-15 起），不再是 1:1 均匀。

    狐缘山间权重最高（0.285）→ 曝光占比最大；未知任务拿保守权重，
    仍能拿到曝光但占比小。黑名单任务单独由 test_spark_pick 覆盖。
    """
    titles = ["狐缘山间", "扫墓丫头，替祖宗横扫天下", "新赛季你的本命英雄是？"]
    counts = {"狐缘": 0, "扫墓": 0, "本命": 0}
    cur = 0
    for _ in range(90):
        name, cur = pick_spark_title(titles, cursor=cur)
        if "狐缘" in name:
            counts["狐缘"] += 1
        elif "扫墓" in name:
            counts["扫墓"] += 1
        elif "本命" in name:
            counts["本命"] += 1
    # 高权重任务曝光最多，且所有候选都能拿到曝光（无饥饿）
    assert counts["狐缘"] > counts["扫墓"] > 0
    assert counts["本命"] > 0
    assert sum(counts.values()) == 90

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
    # 平台需启用：publish_gate 会先拦截停用平台（2026-10-12 起）
    s.platforms = {"kuaishou": PlatformConfig(enabled=True),
                   "douyin": PlatformConfig(enabled=True)}
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
# 标题不再挂专栏前缀（2026-10-18：回到 9 月短句笑点）
# ---------------------------------------------------------------------------

def test_title_keeps_bare_punchline():
    profile = PLATFORM_PROFILES["kuaishou"]
    out = _validate({"title": "没声音却笑到停不下来 这画面太绝了",
                     "description": "d", "tags": ["无声视频", "神反转"],
                     "category": ""}, [], profile)
    assert out["title"] == "没声音却笑到停不下来 这画面太绝了"
    assert "【" not in out["title"]


def test_title_strips_legacy_prefixes():
    """旧专栏前缀一律剥掉，不再改写成另一个前缀。"""
    profile = PLATFORM_PROFILES["kuaishou"]
    cases = {
        "【有点视频】没声音却笑到停不下来": "没声音却笑到停不下来",
        "【无声治愈】暴风雨里互相依偎的小家伙": "暴风雨里互相依偎的小家伙",
        "【无声小剧场】两只小家伙抢玉米": "两只小家伙抢玉米",
        "【纯享放松】安静看完太解压": "安静看完太解压",
        "有点视频没声音也能笑出腹肌": "没声音也能笑出腹肌",
    }
    for raw, expect in cases.items():
        out = _validate({"title": raw, "description": "d",
                         "tags": ["无声视频"], "category": ""}, [], profile)
        assert out["title"] == expect, raw


def test_validate_strips_title_from_description():
    """简介抄标题时校验层先砍掉，避免抖音双框叠句。"""
    profile = PLATFORM_PROFILES["douyin"]
    title = "它自以为走位天衣无缝，直到下一秒啪唧摔出原形"
    out = _validate({"title": title, "description": title + "全程偷感拉满。",
                     "tags": ["无声视频"], "category": ""}, [], profile)
    assert not out["description"].startswith(title)
    assert "全程偷感拉满" in out["description"]


def test_strip_leading_title_exact():
    title = "它迈着六亲不认的步伐，结果下一秒帅不过三秒"
    desc = title + " 走出了最神气的姿势。"
    assert _strip_leading_title(title, desc) == "走出了最神气的姿势。"


def test_strip_leading_title_hook_only():
    title = "它迈着六亲不认的步伐"
    desc = "它迈着六亲不认的步伐，下一刻却当场破功。"
    assert _strip_leading_title(title, desc) == "下一刻却当场破功。"


def test_strip_leading_title_keeps_unique_desc():
    title = "走位翻车"
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
    assert _check_cliche("本以为是个王者，下一秒直接翻车")


def test_cliche_catches_column_prefix():
    assert "专栏前缀" in _check_cliche("【有点视频】没声音却笑到停不下来")


def test_cliche_catches_all_banned_words():
    for w in ("走位", "翻车", "滑跪", "脸刹", "天衣无缝", "王者",
              "帅不过", "下一秒", "自以为", "原地", "打滑", "起飞"):
        assert _check_cliche(f"它{w}了"), w


def test_cliche_catches_bad_opening():
    assert any("开头" in h for h in _check_cliche("它觉得自己藏得很好"))
    assert any("开头" in h for h in _check_cliche("它以为没人看见"))
    assert any("开头" in h for h in _check_cliche("本以为稳了"))


def test_cliche_passes_clean_title():
    """带钩子的短句必须放行，情绪垫句不行。"""
    assert _check_cliche("难怪充电这么慢，墙里那只仓鼠刚被踹醒") == []
    assert _check_cliche("猎豹啃了半天，对方缩成球让它干生气") == []
    assert _check_cliche("没声音却笑到停不下来 这画面太绝了")


def test_cliche_allows_bad_word_mid_sentence():
    """套话词在句中一样拦——它们是词，不是句式。"""
    hits = _check_cliche("这波走位我给满分")
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


# ---------------------------------------------------------------------------
# 视频号登录态完整性（2026-09-29 事故：约 24h 即失效，重扫后复发）
# 实测更正：视频号只设 2 个 cookie（sessionid + wxuin），没有 slave_sid 等。
# ---------------------------------------------------------------------------

class _FakeCtx:
    """模拟 Playwright context，只实现 cookies()。"""

    def __init__(self, pairs):
        self._c = [{"name": n, "value": v} for n, v in pairs]

    def cookies(self, urls=None):
        return self._c


def test_login_complete_accepts_two_cookie_session():
    """视频号真实登录态就是 sessionid + wxuin 两个。"""
    ctx = _FakeCtx([("sessionid", "BgAA9B%2B8"), ("wxuin", "3983570754")])
    assert _login_complete(ctx) is True


def test_login_complete_accepts_extra_cookies():
    """多出其他 cookie 也照样通过。"""
    ctx = _FakeCtx([("sessionid", "x"), ("wxuin", "1"), ("slave_sid", "y")])
    assert _login_complete(ctx) is True


def test_login_complete_requires_sessionid():
    """缺 sessionid 不算登录（wxuin 单独存在可能是残留）。"""
    ctx = _FakeCtx([("wxuin", "1")])
    assert _login_complete(ctx) is False


def test_login_complete_requires_wxuin():
    """缺 wxuin 也不算 —— 它标识账号身份，缺了会发错号。"""
    ctx = _FakeCtx([("sessionid", "x")])
    assert _login_complete(ctx) is False


def test_login_complete_rejects_empty_values():
    """空值 cookie 不计入。"""
    ctx = _FakeCtx([("sessionid", ""), ("wxuin", "")])
    assert _login_complete(ctx) is False


def test_login_complete_handles_broken_context():
    """context 异常不能崩，返回 False。"""
    class _Boom:
        def cookies(self, urls=None):
            raise RuntimeError("boom")
    assert _login_complete(_Boom()) is False


def test_publish_persists_state_via_shared_helper():
    """三个平台的 publish() 都必须回写登录态（用共享助手）。

    注意：实测 sessionid 不轮换，回写不是「续期」机制；这里只保证
    代码保留了该行为，避免未来误删。
    """
    import inspect

    from bot.publish import douyin, kuaishou, weixin
    for mod in (weixin, kuaishou, douyin):
        src = inspect.getsource(mod.publish)
        assert "persist_state_if_changed(" in src, mod.__name__


# ---------------------------------------------------------------------------
# persist_state_if_changed：只在 cookie 真变化时才落盘
# 2026-09-29 事故：无条件回写会用失效 cookie 覆盖好文件并刷新 mtime，
# 制造「刚刚更新过」的假象，误导了第一次诊断。
# ---------------------------------------------------------------------------

class _StateCtx:
    """模拟带 cookies()/storage_state() 的 Playwright context。"""

    def __init__(self, pairs, domain="channels.weixin.qq.com"):
        self._c = [{"name": n, "value": v, "domain": domain} for n, v in pairs]
        self.writes = 0

    def cookies(self, urls=None):
        return self._c

    def storage_state(self, path=None):
        self.writes += 1
        if path:
            import json
            from pathlib import Path
            Path(path).write_text(
                json.dumps({"cookies": self._c, "origins": []}),
                encoding="utf-8")
        return {"cookies": self._c, "origins": []}


def test_persist_writes_when_file_missing(tmp_path):
    """文件不存在 → 应写入。"""
    from bot.publish.base import persist_state_if_changed

    ctx = _StateCtx([("sessionid", "a"), ("wxuin", "1")])
    sp = tmp_path / "s.json"
    assert persist_state_if_changed(ctx, sp, domains=["channels.weixin.qq.com"])
    assert sp.exists()


def test_persist_skips_when_unchanged(tmp_path):
    """cookie 没变 → 不写入（避免刷新 mtime 造成假象）。"""
    from bot.publish.base import persist_state_if_changed

    ctx = _StateCtx([("sessionid", "a"), ("wxuin", "1")])
    sp = tmp_path / "s.json"
    persist_state_if_changed(ctx, sp, domains=["channels.weixin.qq.com"])
    first = ctx.writes
    assert first == 1

    # 同样的 cookie 再来一次 → 不应再写
    assert not persist_state_if_changed(ctx, sp, domains=["channels.weixin.qq.com"])
    assert ctx.writes == 1


def test_persist_writes_when_value_changed(tmp_path):
    """cookie 值变化 → 应写入。"""
    from bot.publish.base import persist_state_if_changed

    ctx = _StateCtx([("sessionid", "a"), ("wxuin", "1")])
    sp = tmp_path / "s.json"
    persist_state_if_changed(ctx, sp, domains=["channels.weixin.qq.com"])

    ctx._c[0]["value"] = "b"      # sessionid 变了
    assert persist_state_if_changed(ctx, sp, domains=["channels.weixin.qq.com"])
    assert ctx.writes == 2


def test_persist_skips_empty_cookies(tmp_path):
    """没有任何 cookie → 不写入（避免用空文件覆盖好文件）。"""
    from bot.publish.base import persist_state_if_changed

    ctx = _StateCtx([])
    sp = tmp_path / "s.json"
    assert not persist_state_if_changed(ctx, sp, domains=["channels.weixin.qq.com"])
    assert not sp.exists()


def test_persist_handles_corrupt_old_file(tmp_path):
    """旧文件损坏 → 直接重写，不崩。"""
    from bot.publish.base import persist_state_if_changed

    sp = tmp_path / "s.json"
    sp.write_text("{ this is not json", encoding="utf-8")
    ctx = _StateCtx([("sessionid", "a"), ("wxuin", "1")])
    assert persist_state_if_changed(ctx, sp, domains=["channels.weixin.qq.com"])


def test_toutiao_registered_and_title_capped():
    """头条标题硬限 30 字，不再保留专栏前缀。"""
    from bot.publish import get_publisher
    from bot.publish.toutiao import (
        TITLE_LIMIT,
        declaration_label,
        fit_title,
        is_dashboard_url,
        is_login_url,
    )

    pub = get_publisher("toutiao")
    assert pub.display_name == "今日头条"
    assert pub.state_path.name == "toutiao_state.json"

    profile = PLATFORM_PROFILES["toutiao"]
    assert profile.max_title_len == TITLE_LIMIT == 30
    long_title = "草地里捡到一只摔懵的小胖龙它居然还在装没事啊真的离谱没声音也笑出腹肌"
    assert len(long_title) > 30
    fitted = fit_title(long_title)
    assert len(fitted) == 30
    assert "【" not in fitted

    out = _validate(
        {"title": long_title, "description": long_title, "tags": [], "category": "忽略"},
        [],
        profile,
    )
    assert len(out["title"]) <= 30
    assert not out["title"].startswith("【")
    assert out["category"] == ""
    assert "无声视频" in out["tags"]
    assert len(out["tags"]) <= 4

    assert declaration_label("AI生成") == "AI生成"
    assert declaration_label("虚构演绎") == "虚构演绎，故事经历"
    assert declaration_label("") == ""
    assert declaration_label("自定义声明") == "自定义声明"

    upload = "https://mp.toutiao.com/profile_v4/xigua/upload-video"
    assert is_dashboard_url(upload)
    assert not is_login_url(upload)
    assert is_login_url("https://mp.toutiao.com/auth/page/login")
    assert is_login_url("https://sso.toutiao.com/login")
    assert not is_dashboard_url("https://mp.toutiao.com/auth/page/login?next=/profile_v4")


def test_toutiao_success_requires_leaving_upload_page():
    """上传页固定文案里就有「审核完成」，停在原页不能算发出去。"""
    from bot.publish.toutiao import account_blocked_reason, publish_succeeded

    upload = "https://mp.toutiao.com/profile_v4/xigua/upload-video"
    body = "正常预计审核完成时间：1 小时内 发布视频"
    assert not publish_succeeded(body, upload, upload)
    assert not publish_succeeded("发布成功", upload, upload)
    manage = "https://mp.toutiao.com/profile_v4/manage/content/all"
    assert publish_succeeded("", manage, upload)
    assert account_blocked_reason("该账号信息未完善，完善后才能发布视频") == "该账号信息未完善"
    assert account_blocked_reason(body) is None


# ---------- 幽灵 kuaishou job 回归（2026-10-28 双平台时代回填 bug） ----------

def _mk_published_task(db, tgt=None):
    """插入一个带 published_at 的 PUBLISHED 任务，返回 task_id。"""
    from bot.db import State
    ts = datetime.now().isoformat(timespec="seconds")
    db.conn.execute(
        """INSERT INTO tasks
           (ig_media_id, shortcode, state, target_platforms, published_at,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("yt_ghost", "yt_ghost", State.PUBLISHED, tgt, ts, ts, ts),
    )
    db.conn.commit()
    return db.conn.execute("SELECT id FROM tasks WHERE shortcode='yt_ghost'").fetchone()["id"]


def test_backfill_no_phantom_job_when_task_has_other_platform_jobs(tmp_path):
    """tgt=kuaishou2 的任务已有 kuaishou2 job：新建 Database() 不得补幽灵 kuaishou job。

    回归背景：单平台时代回填无条件给带 published_at 的任务补 kuaishou job，
    copywriter 的 top_tags 每次生成都现开 Database()，导致 B 号任务在
    published_at 落库后 1 秒内被补一条幽灵 kuaishou PUBLISHED job
    （url=None、created_at==published_at）。实测 task 462-466 五条。
    """
    db = Database(tmp_path / "t.db")
    tid = _mk_published_task(db, tgt="kuaishou2")
    ts = datetime.now().isoformat(timespec="seconds")
    db.conn.execute(
        """INSERT INTO publish_jobs (task_id, platform, state, published_at, created_at, updated_at)
           VALUES (?, 'kuaishou2', 'PUBLISHED', ?, ?, ?)""",
        (tid, ts, ts, ts),
    )
    db.conn.commit()
    db.close()

    # 模拟 copywriter 的 top_tags：重新构造 Database，_migrate 再跑一遍
    db2 = Database(tmp_path / "t.db")
    jobs = db2.jobs(tid)
    db2.close()
    assert [j["platform"] for j in jobs] == ["kuaishou2"], \
        f"不得出现幽灵 kuaishou job：{[j['platform'] for j in jobs]}"


def test_backfill_still_fills_single_platform_era_tasks(tmp_path):
    """零 job 的存量任务（单平台时代）：回填语义保留，仍补 kuaishou job。"""
    db = Database(tmp_path / "t.db")
    tid = _mk_published_task(db)
    db.close()

    db2 = Database(tmp_path / "t.db")
    jobs = db2.jobs(tid)
    db2.close()
    assert [j["platform"] for j in jobs] == ["kuaishou"]
    assert jobs[0]["state"] == JobState.PUBLISHED

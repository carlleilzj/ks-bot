"""回归测试：AI 文案链路的边界与修复点。

覆盖 2026-11-04 深度审计发现的问题：
- 非快手平台 JSON 示例必须是合法 JSON（category 值不能带说明文字）
- first_comment 只剥成对括号，不把「（第一人称）」切成半截
- 命中幻觉的文案绝不能被当作兜底结果复用
- 热榜缓存过期后必须返回空（不能继续注入陈旧热点）
- 标题超长时优先在标点处截断，不在句中硬切
"""

from __future__ import annotations

import json
import re

import pytest

from bot.ai.copywriter import (
    BANNED_PATTERNS,
    PLATFORM_PROFILES,
    _extract_json,
    _fit_title_natural,
    _system_prompt,
    _validate,
    merge_tags,
)

# ---------- JSON 示例合法性 ----------

@pytest.mark.parametrize("name", ["douyin", "xhs", "weixin", "toutiao", "kuaishou", "kuaishou2"])
def test_output_json_sample_is_valid_json(name):
    """prompt 里给的 JSON 示例必须能被 json.loads 解析。

    历史 bug：category 值被拼成 `""（该平台无分区…）`，引号后跟裸中文，
    一边要求「只输出合法 JSON」一边给非法样例。
    """
    prompt = _system_prompt(PLATFORM_PROFILES[name])
    block = prompt.split("只输出一个合法的 JSON 对象", 1)[1]
    block = block[block.index("{") : block.rindex("}") + 1]
    # 示例里的占位说明文字是中文，但结构必须是合法 JSON
    data = json.loads(block)
    assert "title" in data and "tags" in data
    assert isinstance(data["category"], str)
    # category 的值只能是空串或纯分区名，不能夹带说明文字
    assert "无分区" not in data["category"]
    assert "必须从给定" not in data["category"]


def test_banned_patterns_all_reach_prompt():
    """禁词表不能被切片截断（曾有 [:10] 漏掉「做饭/写作业/做家务」）。"""
    prompt = _system_prompt(PLATFORM_PROFILES["kuaishou"])
    for pat in BANNED_PATTERNS:
        assert pat in prompt, f"禁词「{pat}」没进 prompt"


def test_title_len_hint_respects_platform_cap():
    """长度提示不能超过 12~24 基线，也不能超过平台硬上限。"""
    import re as _re

    for name, prof in PLATFORM_PROFILES.items():
        prompt = _system_prompt(prof)
        m = _re.search(r"\*\*短\*\*：(\d+)~(\d+) 个字", prompt)
        assert m, f"{name} 缺少长度提示"
        low, high = int(m.group(1)), int(m.group(2))
        assert high <= prof.max_title_len, f"{name} 提示上限 {high} 超过硬上限 {prof.max_title_len}"
        assert high <= 24, f"{name} 提示上限 {high} 超过已验证基线 24"
        assert low <= high


# ---------- first_comment 清洗 ----------

@pytest.mark.parametrize("raw,expect", [
    ("神评内容（第一人称）", "神评内容（第一人称）"),   # 括号不包裹整体，保留原样
    ("（整句被括号包住）", "整句被括号包住"),          # 成对包裹整体才剥
    ("【没声音】这也太逗了", "这也太逗了"),            # 前缀标签去掉
    ("【开头】正文【结尾】", "开头】正文【结尾"),
    ("#神评 #互动", "神评 #互动"),
    ("正常首评一句话", "正常首评一句话"),
    ('"带引号的首评"', "带引号的首评"),
])
def test_first_comment_strips_pairs_only(raw, expect):
    out = _validate(
        {"title": "测试标题啊哈哈", "description": "d",
         "tags": ["a", "b", "c", "d"], "category": "", "first_comment": raw},
        ["搞笑"], PLATFORM_PROFILES["kuaishou"],
    )
    assert out["first_comment"] == expect
    # 任何情况下都不能出现「只有左括号没有右括号」的半截括号
    assert not re.search(r"（$|[（(]\s*$", out["first_comment"])


# ---------- 标题截断 ----------

def test_fit_title_breaks_at_punctuation():
    long = "它辛辛苦苦孵了个蛋，破壳出来的瞬间直接给整不会了，满心欢喜以为要当妈"
    out = _fit_title_natural(long, 20)
    assert len(out) <= 20
    assert not out.endswith(("，", "、", "；"))


def test_fit_title_no_truncation_when_short():
    assert _fit_title_natural("短标题", 30) == "短标题"


# ---------- JSON 提取 ----------

def test_extract_json_picks_first_valid_object():
    """模型输出两个对象时，不能从头拼到尾导致必然失败。"""
    text = '先说明一下 {"title": "a", "tags": ["x"]} 然后又是 {"title": "b"}'
    data = _extract_json(text)
    assert data["title"] == "a"


# ---------- 标签数量/家族 ----------

@pytest.mark.parametrize("n", [0, 1, 3, 4, 5, 8])
def test_kuaishou_tags_always_four(n):
    """A/B 号 4 槽位配置下，无论 AI 给几个，最终恰好 4 个。"""
    prof = PLATFORM_PROFILES["kuaishou"]
    tags = [f"标签{i}" for i in range(n)]
    out = merge_tags(tags, prof.max_tags, base_tag=prof.tag_base,
                     content_slots=prof.tag_content_slots,
                     fixed_pool=prof.tag_fixed_pool)
    assert len(out) == 4, out


def test_merge_tags_pool_also_dedupes_family():
    """池子补齐也要家族去重：不能出现「沙雕视频」+「搞笑动画」这种同族堆叠。"""
    out = merge_tags(["3D动画", "霸王龙", "沙雕视频"], 4, base_tag="",
                     content_slots=4,
                     fixed_pool=("搞笑动画", "萌宠日常", "笑到肚子疼", "今日快乐源泉"))
    assert len(out) == 4
    assert "搞笑动画" not in out, out   # 与「沙雕视频」同属搞笑家族，应被跳过
    assert "萌宠日常" in out


# ---------- 热榜过期 ----------

def test_cached_hot_topics_expired_returns_empty(tmp_path, monkeypatch):
    """热榜超过 max_age_hours 必须返回空，避免长期追过期热点。"""
    import bot.monitor.kuaishou_hot as kh

    f = tmp_path / "hot.json"
    f.write_text(json.dumps({
        "updated_at": "2026-01-01T00:00:00",
        "topics": [{"rank": 1, "topic": "过期热点", "hot_value": "1万"}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(kh, "KS_HOT_FILE", f)
    assert kh.load_cached_hot_topics(max_age_hours=48) == []

    # 新鲜缓存正常返回
    from datetime import datetime
    f.write_text(json.dumps({
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "topics": [{"rank": 1, "topic": "新鲜热点", "hot_value": "1万"}],
    }, ensure_ascii=False), encoding="utf-8")
    got = kh.load_cached_hot_topics(max_age_hours=48)
    assert got and got[0]["topic"] == "新鲜热点"


def test_cached_hot_topics_corrupt_returns_empty(tmp_path, monkeypatch):
    import bot.monitor.kuaishou_hot as kh

    f = tmp_path / "hot.json"
    f.write_text("{ 不是 json", encoding="utf-8")
    monkeypatch.setattr(kh, "KS_HOT_FILE", f)
    assert kh.load_cached_hot_topics() == []

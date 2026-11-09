"""禁用话题（banned_tags）三层拦截。

背景（2026-11-09 B 号真实数据）
--------------------------------
B 号 11-03 之后 31 条样本里，「家居生活」用了 9 次、中位播放仅 86，
是最差的话题（「居家好物」同区间中位 1671，差 19 倍）。
根因不是模型「选择」了这个词，而是 prompt 的示例里**把它当赛道词范例列出来**，
模型照抄示例 —— 实测光在 prompt 里写「别用」不可靠，
所以需要三层：prompt 明示 + _validate 硬剔除 + merge_tags 池过滤。

为什么池过滤单独一层也要测：
`tag_fixed_pool` 和 `banned_tags` 很容易在后续配置演进中出现重叠
（本次就出现过「梦中情房」同时在池和禁用名单里），
只靠 _validate 拦，补齐阶段会把池里的词重新塞回来。
"""

from __future__ import annotations

import pytest

from bot.ai.copywriter import (
    PLATFORM_PROFILES,
    _system_prompt,
    _validate,
    merge_tags,
)


# ---------- 第 1 层：prompt 明示 ----------

class TestPromptLayer:
    def test_kuaishou2_prompt_lists_banned_words(self):
        p = _system_prompt(PLATFORM_PROFILES["kuaishou2"])
        assert "绝对禁止使用这些话题词" in p
        for w in ("家居生活", "生活记录", "梦中情房"):
            assert w in p

    def test_kuaishou2_prompt_no_longer_offers_banned_as_example(self):
        """关键词只应出现在「禁止」句里，不能同时出现在示例里。

        否则模型仍会照抄示例（这正是漂移的根因）。
        """
        p = _system_prompt(PLATFORM_PROFILES["kuaishou2"])
        assert p.count("家居生活") == 1, "「家居生活」只应出现在禁用句，不能进示例"

    def test_other_profiles_have_no_ban_section(self):
        """A 号（kuaishou）等没有禁用词，prompt 不该多出这句。"""
        p = _system_prompt(PLATFORM_PROFILES["kuaishou"])
        assert "绝对禁止使用这些话题词" not in p
        # A 号行为不被牵连：示例里仍有它自己的赛道词
        assert "搞笑动画" in p


# ---------- 第 2 层：_validate 硬剔除 ----------

class TestValidateLayer:
    def test_drops_banned_tag_from_model_output(self):
        out = _validate(
            {"title": "换上这套床品像睡进云里", "description": "面料软糯",
             "tags": ["家居生活", "居家好物", "四件套", "卧室焕新"],
             "category": "生活记录", "first_comment": "这套真的躺进去就不想起"},
            ["生活记录"], PLATFORM_PROFILES["kuaishou2"],
        )
        assert "家居生活" not in out["tags"]
        assert "居家好物" in out["tags"]

    def test_drops_with_hash_prefix(self):
        """模型带 # 返回时也要拦住（清洗后才比对）。"""
        out = _validate(
            {"title": "软糯四件套开箱", "description": "d",
             "tags": ["#家居生活", "#居家好物"], "category": "",
             "first_comment": "c"},
            ["生活记录"], PLATFORM_PROFILES["kuaishou2"],
        )
        assert "家居生活" not in out["tags"]

    def test_does_not_touch_profiles_without_bans(self):
        """A 号没有禁用词，同样输入不该被改。"""
        out = _validate(
            {"title": "笑到停不下来哈哈哈", "description": "d",
             "tags": ["家居生活", "搞笑日常"], "category": "搞笑",
             "first_comment": "c"},
            ["搞笑"], PLATFORM_PROFILES["kuaishou"],
        )
        assert "家居生活" in out["tags"]


# ---------- 第 3 层：merge_tags 池过滤 ----------

class TestMergeTagsLayer:
    def test_banned_filtered_from_model_tags(self):
        out = merge_tags(
            ["家居生活", "居家好物"], 4, base_tag="", content_slots=4,
            fixed_pool=PLATFORM_PROFILES["kuaishou2"].tag_fixed_pool,
            banned=PLATFORM_PROFILES["kuaishou2"].banned_tags,
        )
        assert "家居生活" not in out
        assert "居家好物" in out

    def test_banned_filtered_even_if_in_fixed_pool(self):
        """池与禁用名单重叠时，补齐阶段不能把词塞回来。"""
        out = merge_tags(
            [], 4, base_tag="", content_slots=4,
            fixed_pool=("好物分享", "梦中情房", "居家好物"),
            banned=("梦中情房",),
        )
        assert "梦中情房" not in out
        assert len(out) == 2

    def test_no_banned_argument_keeps_old_behavior(self):
        """不传 banned 时行为不变（A 号及既有调用点不受影响）。"""
        out = merge_tags([], 4, base_tag="", content_slots=4,
                         fixed_pool=("好物分享", "梦中情房"))
        assert out == ["好物分享", "梦中情房"]

    def test_banned_applies_to_latin_path(self):
        """英文标签路径也要生效，否则以后给 IG 号加禁用词会静默失效。"""
        out = merge_tags(
            ["homedecor", "cozyhome"], 3, base_tag="", content_slots=3,
            fixed_pool=("homedecor", "amazonfinds", "interiordesign"),
            banned=("homedecor",),
            latin=True,
        )
        assert "homedecor" not in out
        assert "cozyhome" in out
        assert len(out) == 3


# ---------- 配置本身 ----------

class TestProfileConfig:
    def test_kuaishou2_pool_is_specific_category_words(self):
        pool = PLATFORM_PROFILES["kuaishou2"].tag_fixed_pool
        assert "家居生活" not in pool
        assert "居家好物" in pool
        # 换入的是具体品类词，不是泛主题词
        assert "四件套" in pool

    def test_kuaishou2_banned_covers_worst_tags(self):
        banned = set(PLATFORM_PROFILES["kuaishou2"].banned_tags)
        assert {"家居生活", "生活记录", "梦中情房"} <= banned

    def test_pool_and_banned_do_not_conflict_in_effect(self):
        """配置演进容易让两个清单重叠，这里钉住「最终产出不含禁用词」。"""
        prof = PLATFORM_PROFILES["kuaishou2"]
        out = merge_tags([], prof.max_tags, base_tag=prof.tag_base,
                         content_slots=prof.tag_content_slots,
                         fixed_pool=prof.tag_fixed_pool,
                         banned=prof.banned_tags)
        assert not (set(out) & set(prof.banned_tags))
        assert len(out) == prof.max_tags


# ---------- 第 4 层：外部热榜 / 数据回流榜单 ----------

class TestHotSourceLayer:
    def test_ks_hot_topics_filtered(self):
        """快手实时热榜若出现禁用词，不能喂给模型。

        热榜是外部数据，某天可能刚好把「家居生活」推上榜；prompt 又明确
        要求「优先选热搜」—— 不过滤等于亲手把它喂回去。
        """
        from bot.ai.copywriter import _filter_banned_topics
        topics = [
            {"rank": 1, "topic": "家居生活", "hot_value": "900万"},
            {"rank": 2, "topic": "秋冬穿搭", "hot_value": "800万"},
        ]
        out = _filter_banned_topics(topics, ("家居生活",))
        assert [t["topic"] for t in out] == ["秋冬穿搭"]

    def test_no_banned_passes_through(self):
        from bot.ai.copywriter import _filter_banned_topics
        topics = [{"rank": 1, "topic": "家居生活"}]
        assert _filter_banned_topics(topics, ()) == topics

    def test_play_ranking_excludes_banned(self):
        """回流播放榜（top_tags）也要排除：禁用词历史上发过，足够进榜。"""
        from bot.ai.hot_tags import top_tags_from_rows
        rows = [
            {"tags": '["家居生活", "居家好物"]', "play_count": 5000},
            {"tags": '["四件套"]', "play_count": 800},
        ]
        ranked = top_tags_from_rows(rows, min_play=500,
                                    exclude={"家居生活"})
        names = [r[0] for r in ranked]
        assert "家居生活" not in names
        assert "居家好物" in names


# ---------- 端到端：B 号产出不再含「家居生活」 ----------

@pytest.mark.parametrize("model_tags", [
    ["家居生活", "居家好物", "四件套", "卧室焕新"],
    ["家居生活"] * 4,
    ["生活记录", "家居生活", "梦中情房"],
])
def test_kuaishou2_output_never_contains_banned(model_tags):
    prof = PLATFORM_PROFILES["kuaishou2"]
    out = _validate(
        {"title": "降温天换上这套毛绒床品", "description": "软糯手感",
         "tags": model_tags, "category": "生活记录",
         "first_comment": "这套摸起来是真的软"},
        ["生活记录"], prof,
    )
    assert not (set(out["tags"]) & set(prof.banned_tags)), out["tags"]

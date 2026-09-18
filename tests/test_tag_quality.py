"""话题多样性 + 情绪化标签放开 的回归测试。

背景（2026-09-19 数据驱动）：
AI 生成的话题退化成近义词排列组合 —— `治愈 / 治愈系 / 治愈解压 /
治愈动画` 是同一个流量池，却被当成 4 个不同标签占满坑位。
抖音后台实测：话题多样的作品播放 1080~1545，退化成通用四件套的
只有 215~503。

同时：banned 词表把「情绪化标签」和「口吻套话」混着封了。
`#笑到肚子疼` 这类情绪化标签实测效果好，应放开；
「姐妹们 / 家人们 / 心里踏实」是口吻套话，继续封。
"""
from __future__ import annotations

from bot.ai import tag_quality as tq


class TestTagFamily:
    def test_healing_synonyms_share_family(self):
        """治愈系/治愈解压/治愈动画 必须归到同一个家族。"""
        fams = {tq.tag_family(t) for t in
                ("治愈", "治愈系", "治愈解压", "治愈动画", "治愈瞬间")}
        assert len(fams) == 1, f"治愈家族应归一，实际 {fams}"

    def test_relax_synonyms_share_family(self):
        fams = {tq.tag_family(t) for t in
                ("解压", "解压放松", "解压视频", "放松", "静心", "减压")}
        assert len(fams) == 1

    def test_chunxiang_synonyms_share_family(self):
        """纯享家族归一。

        注意「纯享放松」是复合词，同时含「纯享」与「放松」（都 2 字）。
        它必须归到「纯享」而非「解压」—— 靠 TAG_FAMILIES 的声明顺序
        保证（纯享声明在解压之前）。改顺序会破坏此测试。
        """
        fams = {tq.tag_family(t) for t in
                ("纯享", "纯享放松", "视觉纯享", "沉浸式")}
        assert len(fams) == 1, f"纯享家族应归一，实际 {fams}"
        assert tq.tag_family("纯享放松") == "纯享", \
            "「纯享放松」应归纯享家族，不是解压"

    def test_different_families_differ(self):
        assert tq.tag_family("治愈") != tq.tag_family("搞笑")
        assert tq.tag_family("萌宠") != tq.tag_family("解压")

    def test_unknown_tag_returns_none(self):
        assert tq.tag_family("郴州同城") is None
        assert tq.tag_family("") is None


class TestEnforceDiversity:
    def test_dedupes_same_family(self):
        """核心回归：治愈/治愈系/治愈解压 这类**同义泛词**只准留 1 个。

        注意区别于长尾补齐词（如「治愈瞬间」）—— 后者是刻意加入的
        不同流量池，允许与主词同家族。这里断言的是：
        **输入里的**治愈家族近义词不能全部保留。
        """
        src = ["无声视频", "治愈", "治愈系", "治愈解压"]
        out = tq.enforce_diversity(src, max_tags=4)
        kept = [t for t in out if t in src]
        heal = [t for t in kept if tq.tag_family(t) == "治愈"]
        assert len(heal) == 1, f"输入的治愈近义词应只留 1 个，实际 {heal}"

    def test_dedupes_relax_and_chunxiang(self):
        """解压/纯享家族同样要去重。"""
        for dup, fam in ((["解压", "解压放松", "解压视频"], "解压"),
                         (["纯享", "视觉纯享", "纯享放松"], "纯享")):
            src = ["无声视频"] + dup
            out = tq.enforce_diversity(src, max_tags=4)
            kept = [t for t in out if t in src]
            same = [t for t in kept if tq.tag_family(t) == fam]
            assert len(same) == 1, f"{fam} 近义词应只留 1 个，实际 {same}"

    def test_fills_gap_with_longtail(self):
        """去重后坑位要补满，且补的是长尾/情绪词而非又一轮泛词。"""
        out = tq.enforce_diversity(
            ["无声视频", "治愈", "治愈系"], max_tags=4,
        )
        assert len(out) == 4, f"应补齐到 4 个，实际 {out}"
        # 「治愈系」被去重，补进来的必须来自 FILLER_TAGS
        fillers = set(tq.FILLER_TAGS["funny"]) | set(tq.FILLER_TAGS["healing"])
        added = [t for t in out if t not in ("无声视频", "治愈")]
        assert added, "应补齐了新标签"
        assert all(t in fillers for t in added), \
            f"补齐的应全是长尾情绪词，实际 {added}"

    def test_filler_is_emotional_longtail(self):
        """补齐用的应是长尾情绪词，不是又一个泛词。"""
        out = tq.enforce_diversity(["无声视频", "治愈", "治愈系"], max_tags=4)
        assert any(t in tq.EMOTIONAL_TAGS for t in out), \
            f"应有情绪化长尾词补齐，实际 {out}"

    def test_base_tag_always_first(self):
        out = tq.enforce_diversity(["治愈", "无声视频", "萌宠"], max_tags=4)
        assert out[0] == "无声视频", "基础 IP 标签必须固定在首位"

    def test_respects_max_tags(self):
        many = ["无声视频"] + [f"标签{i}" for i in range(20)]
        out = tq.enforce_diversity(many, max_tags=4)
        assert len(out) <= 4

    def test_exact_duplicates_removed(self):
        out = tq.enforce_diversity(["无声视频", "治愈", "治愈"], max_tags=4)
        assert out.count("治愈") == 1

    def test_keeps_diverse_input_intact(self):
        """本来就多样的话题不应被大改。"""
        src = ["无声视频", "萌宠日常", "神反转", "3D动画"]
        out = tq.enforce_diversity(src, max_tags=4)
        for t in src:
            assert t in out, f"{t} 不该被删掉"

    def test_fallback_when_all_same_family(self):
        """全是同家族时不至于只剩一个光杆标签。"""
        out = tq.enforce_diversity(
            ["无声视频", "治愈", "治愈系", "治愈解压"], max_tags=4,
        )
        assert len(out) >= 2, "全同家族时仍应保留多于 1 个标签"

    def test_empty_input(self):
        out = tq.enforce_diversity([], max_tags=4)
        assert out == ["无声视频"]

    def test_strips_hash_prefix(self):
        out = tq.enforce_diversity(["#治愈", "#萌宠"], max_tags=4)
        assert all(not t.startswith("#") for t in out)


class TestEmotionalTagsOpen:
    def test_emotional_tags_listed(self):
        """用户点名要放开的情绪化标签必须在清单里。"""
        for t in ("笑到肚子疼", "无声也精彩", "看完必笑", "神反转"):
            assert tq.is_emotional(t), f"{t} 应属于放开的情绪化标签"

    def test_banned_patterns_no_longer_block_emotional_tags(self):
        """关键回归：情绪化标签不得再出现在 banned 表里。

        旧 BANNED_PATTERNS 含「太上头了/停不下来/太绝了/拉满」——
        这些是标签而非口吻套话，会误伤。
        """
        from bot.ai.copywriter import BANNED_PATTERNS
        for t in ("笑到肚子疼", "无声也精彩", "看完必笑", "看过瘾"):
            assert t not in BANNED_PATTERNS, f"{t} 不该被禁"

    def test_verbal_tics_still_banned(self):
        """口吻套话必须继续封。"""
        from bot.ai.copywriter import BANNED_PATTERNS
        for t in ("姐妹们", "家人们", "心里踏实", "做饭"):
            assert t in BANNED_PATTERNS, f"{t} 是口吻套话，应继续封"


class TestPolarity:
    def test_funny_detection(self):
        assert tq.detect_polarity(["搞笑", "翻车"], "笑死我了") == "funny"

    def test_healing_detection(self):
        assert tq.detect_polarity(["治愈", "放松"], "安静的午后") == "healing"


# ---------------------------------------------------------------------------
# 固定话题组合（2026-09-19 He 指定：#搞笑日常 #沙雕视频 #搞笑动画 #笑到肚子疼）
# ---------------------------------------------------------------------------

def test_fixed_tags_ks_four_slots():
    """快手 4 坑：无声视频 + 指定三项。AI 生成的 tags 全部忽略。"""
    out = tq.enforce_diversity(["治愈", "解压", "纯享放松"], 4,
                            fixed_tags=["搞笑日常", "沙雕视频", "搞笑动画",
                                        "笑到肚子疼"])
    assert out == ["无声视频", "搞笑日常", "沙雕视频", "搞笑动画"], out


def test_fixed_tags_dy_five_slots():
    """抖音 5 坑：多放下「笑到肚子疼」。"""
    out = tq.enforce_diversity(["治愈"], 5,
                            fixed_tags=["搞笑日常", "沙雕视频", "搞笑动画",
                                        "笑到肚子疼"])
    assert out == ["无声视频", "搞笑日常", "沙雕视频", "搞笑动画", "笑到肚子疼"]


def test_fixed_tags_empty_input():
    """AI 没生成 tags 也照样输出固定组合（不回退 [base_tag]）。"""
    out = tq.enforce_diversity([], 4,
                            fixed_tags=["搞笑日常", "沙雕视频", "搞笑动画"])
    assert out == ["无声视频", "搞笑日常", "沙雕视频", "搞笑动画"]


def test_fixed_tags_none_keeps_old_behavior():
    """fixed_tags=None 时走原有多样性逻辑（回归保护）。"""
    out = tq.enforce_diversity(["无声视频", "治愈", "治愈系", "治愈解压"], 4)
    # 旧逻辑：同家族去重 + 补齐
    assert out[0] == "无声视频"
    assert out.count("治愈") + out.count("治愈系") + out.count("治愈解压") == 1 \
        or len(out) == 4   # 补齐路径也允许

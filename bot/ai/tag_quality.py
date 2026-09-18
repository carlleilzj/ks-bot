"""话题（hashtag）质量控制：多样性校验 + 情绪化标签开放。

背景（2026-09-19 实测数据驱动）

问题：AI 生成的话题退化成近义词排列组合。
    待发作品标签池实际长这样：
        #无声视频 治愈 纯享 解压
        #无声视频 纯享放松 治愈系 解压
        #无声视频 纯享放松 视觉解压 治愈系
        #无声视频 治愈 纯享解压 动画短片
    `治愈 / 治愈系 / 治愈解压 / 治愈动画` 在词义上是**同一个流量池**，
    却被当成 4 个不同标签占满坑位 → 实际只投递了 1 个池子。

对比抖音后台真实播放量（同账号）：
    话题多样（含 #静音纯享 #萌宠日常 #细节控必看 #神反转）→ 1080~1545
    话题退化成通用四件套                                    → 215~503
所以多样性不是玄学，是能测出来的。

另外 banned 词表把「情绪化标签」和「口吻套话」混在一起封了：
    「太上头了 / 停不下来 / 太绝了 / 拉满」是**标签**，早期
    `#笑到肚子疼`、`#无声也精彩` 这类情绪化标签播放明显更好；
    而「姐妹们 / 家人们 / 心里踏实」是**口吻套话**，确实该封。
本模块把二者分开：口吻套话继续封，情绪化标签放开。

设计原则
    1. 词干归一化：`治愈系 / 治愈解压 / 治愈动画` 都归到 `治愈` 家族，
       同家族最多保留 1 个（除非全部都是同家族，此时保留最高分的）。
    2. 缺位补齐：被去掉的近义词用**长尾情绪词**补回来，而不是留空。
    3. 纯函数、无副作用，方便单测。
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 词干家族：同家族的标签视为同一个流量池，不应重复占位
# ---------------------------------------------------------------------------

# 键为家族名，值为该家族包含的"词根"（用 in 判断，做子串匹配）
#
# ⚠️ 声明顺序有意义：词根长度相同时，**先声明的家族胜出**。
# 「纯享放松」同时含「纯享」与「放松」（都 2 字），它应归到「纯享」
# 而非「解压」—— 所以纯享家族必须声明在解压之前。
# 改这个 dict 的顺序会改变 tag_family 的结果，有测试锁定。
TAG_FAMILIES: dict[str, tuple[str, ...]] = {
    "纯享": ("纯享", "沉浸", "视觉享受", "视觉盛", "唯美", "丝滑"),
    "治愈": ("治愈", "疗愈", "暖心", "温馨", "温情"),
    "解压": ("解压", "减压", "放松", "舒缓", "松弛", "静心", "静气"),
    "无声": ("无声", "默片", "静音", "安静"),
    "动画": ("动画", "3d", "三维", "卡通", "动漫"),
    "萌宠": ("萌宠", "萌物", "小动物", "猫咪", "小猫", "狗狗", "小狗", "兔子", "兔兔"),
    "搞笑": ("搞笑", "幽默", "沙雕", "欢乐", "逗趣", "滑稽"),
}


def tag_family(tag: str) -> str | None:
    """返回标签所属家族名；不属于任何家族返回 None。

    匹配规则：**按词根长度降序**取第一个命中。原因：`纯享放松` 同时含
    「纯享」和「放松」，若按 dict 顺序匹配会落到「解压」家族（实测过），
    让同一个词在两次调用间归类不一致。

    长度相同时按 TAG_FAMILIES 声明顺序决定，保证确定性。
    注意「纯享放松」这类复合词，判定顺序是 纯享 > 放松（同长时靠声明序，
    纯享家族声明在解压之前）。
    """
    t = (tag or "").strip().lower()
    if not t:
        return None
    best: tuple[int, int, str] | None = None
    for order, (fam, roots) in enumerate(TAG_FAMILIES.items()):
        for r in roots:
            if r in t:
                key = (len(r), -order)
                if best is None or key > (best[0], best[1]):
                    best = (len(r), -order, fam)
    return best[2] if best else None


# ---------------------------------------------------------------------------
# 情绪化标签开放清单
# ---------------------------------------------------------------------------

# 这些是**情绪化 / 网感标签**，曾经效果好（#笑到肚子疼 等），
# 不属于"口吻套话"，检测时放行。
EMOTIONAL_TAGS: tuple[str, ...] = (
    "笑到肚子疼", "笑到停不下来", "笑不活了", "笑死我了", "笑出腹肌",
    "无声也精彩", "无声也上头", "无声胜有声", "无声神作", "无声神操作",
    "看完必笑", "看一遍不够", "值得反复看", "太上头了", "根本停不下来",
    "细节控必看", "细节控", "神反转", "高能预警", "名场面",
    "解压神器", "治愈天花板", "萌到犯规", "可爱暴击",
    "莫名其妙就想笑", "快乐源泉", "治愈瞬间", "意外惊喜",
)

# 用于补齐空缺的长尾情绪词（按内容情感极性分组）
FILLER_TAGS: dict[str, tuple[str, ...]] = {
    "funny": ("笑到肚子疼", "无声也精彩", "神反转", "看完必笑",
              "名场面", "笑到停不下来", "快乐源泉"),
    "healing": ("治愈瞬间", "无声胜有声", "细节控必看", "萌到犯规",
                "看一遍不够", "意外惊喜", "无声也上头"),
}


def _norm(tag: str) -> str:
    return re.sub(r"\s+", "", (tag or "").lstrip("#").strip())


def detect_polarity(tags: list[str], title: str = "") -> str:
    """判断内容偏搞笑还是偏治愈，用于挑补齐词。"""
    blob = " ".join(tags) + " " + (title or "")
    funny_hits = sum(1 for k in ("搞笑", "幽默", "沙雕", "翻车", "笑", "逗", "滑稽")
                     if k in blob)
    heal_hits = sum(1 for k in ("治愈", "放松", "纯享", "安静", "暖", "解压")
                    if k in blob)
    return "funny" if funny_hits > heal_hits else "healing"


# ---------------------------------------------------------------------------
# 主校验：去重 + 补齐
# ---------------------------------------------------------------------------

def enforce_diversity(
    tags: list[str],
    max_tags: int,
    title: str = "",
    base_tag: str = "无声视频",
) -> list[str]:
    """让话题保持多样性：同家族去重，空缺用长尾情绪词补齐。

    参数
        tags      —— 原始标签（不含 #）
        max_tags  —— 该平台标签上限
        title     —— 用于判断情感极性
        base_tag  —— 固定占位的基础 IP 标签（默认「无声视频」）

    返回
        新的标签列表，长度 <= max_tags，**已去家族重复**。

    规则
        1. base_tag 始终保留在第 1 位（账号辨识度）。
        2. 其余位置：同名去重，同家族只留第 1 个。
        3. 若去掉后数量不足，用 FILLER_TAGS 按极性补齐。
        4. 若全部标签都属于**同一个**家族且无其他家族可选，
           则保留原样（避免把唯一有效信息也删掉）。
    """
    clean: list[str] = []
    for t in tags or []:
        n = _norm(t)
        if n and n not in clean:
            clean.append(n)

    # 输入为空：只回基础标签，不凭空补一堆情绪词
    # （否则空输入会返回 4 个标签，掩盖上游生成失败）
    if not clean:
        return [base_tag]

    # base_tag 提到首位
    if base_tag in clean:
        clean.remove(base_tag)
    result = [base_tag]

    used_families: dict[str, str] = {}   # 家族 -> 首次出现的标签
    dropped: list[str] = []

    others = [t for t in clean if t != base_tag]
    for t in others:
        if len(result) >= max_tags:
            break
        fam = tag_family(t)
        if fam and fam in used_families:
            dropped.append(f"{t}（与「{used_families[fam]}」同属 {fam} 家族）")
            continue
        if fam:
            used_families[fam] = t
        result.append(t)

    # 若因为去重导致坑位没填满，用长尾情绪词补齐。
    # 注意：补齐词**豁免家族检查** —— 它们是刻意挑选的长尾变体
    # （如「治愈瞬间」与「治愈」同家族但属不同流量池），
    # 若按家族过滤会被自己的主词挤掉，导致补不上。
    if len(result) < max_tags:
        polarity = detect_polarity(result + dropped, title)
        for filler in FILLER_TAGS[polarity]:
            if len(result) >= max_tags:
                break
            if filler in result:
                continue
            result.append(filler)

    # 极端兜底：去重后只剩 base_tag（说明原标签全是同家族近义词）
    # —— 这时保留原始标签，总比只剩一个光杆标签强
    if len(result) <= 1 and len(clean) > 1:
        log.debug("话题去重后仅剩基础标签，回退原列表：%s", tags)
        return (clean or [base_tag])[:max_tags]

    if dropped:
        log.info("话题多样性调整：去掉 %s → 最终 %s",
                 "；".join(dropped), " ".join("#" + t for t in result))
    return result[:max_tags]


def is_emotional(tag: str) -> bool:
    """该标签是否属于放开的情绪化标签。"""
    return _norm(tag) in EMOTIONAL_TAGS

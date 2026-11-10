"""跨海搬运硬隔离法则（Cross-Border Routing Boundary）。

业务准则（2026-11-11 确定为系统级硬约束）：
1. **出海链路（目标含 ig_* 或 youtube）**：
   - 只能消费【国内跨平台素材】（douyin, xhs, kuaishou, bilibili, toutiao, weibo）。
   - 严禁海外素材流向出海链路（坚决禁止 IG 搬 IG、YouTube 搬 YouTube 等同台或海外内卷搬运）。
2. **国内链路（目标含 kuaishou, kuaishou2, douyin, xhs, weixin, toutiao）**：
   - 只能消费【海外平台素材】（instagram, youtube, tiktok, facebook, twitter）。
   - 严禁国内素材流向国内链路（坚决禁止同台搬运/国内相互搬运，避免营销打标或侵权下架）。
"""

from __future__ import annotations

# 国内源平台
DOMESTIC_SOURCES = {"douyin", "xhs", "kuaishou", "bilibili", "toutiao", "weibo"}

# 海外源平台
OVERSEAS_SOURCES = {"instagram", "youtube", "tiktok", "facebook", "twitter"}

# 国内目标发布平台
DOMESTIC_TARGETS = {"kuaishou", "kuaishou2", "douyin", "xhs", "weixin", "toutiao"}

# 出海目标发布平台
OVERSEAS_TARGETS = {"ig_pets", "ig_satisfying", "ig_anim", "ig_home", "youtube"}


def is_overseas_target(platform: str) -> bool:
    """是否属于出海发布目标平台。"""
    p = (platform or "").strip().lower()
    return p in OVERSEAS_TARGETS or p.startswith("ig_")


def is_domestic_target(platform: str) -> bool:
    """是否属于国内发布目标平台。"""
    p = (platform or "").strip().lower()
    return p in DOMESTIC_TARGETS or p.startswith("kuaishou")


def is_domestic_source(platform: str) -> bool:
    """是否属于国内来源素材平台。"""
    p = (platform or "").strip().lower()
    return p in DOMESTIC_SOURCES


def is_overseas_source(platform: str) -> bool:
    """是否属于海外来源素材平台。"""
    p = (platform or "").strip().lower()
    return p in OVERSEAS_SOURCES


def validate_cross_border(source_platform: str, target_platform: str) -> tuple[bool, str]:
    """校验单对 (来源平台, 目标发布平台) 是否符合跨海搬运硬隔离法则。
    
    返回 (是否合规, 违规原因)。
    """
    sp = (source_platform or "").strip().lower()
    tp = (target_platform or "").strip().lower()

    if is_overseas_target(tp):
        # 出海目标：只能搬国内素材
        if not is_domestic_source(sp):
            return (
                False,
                f"出海平台 [{tp}] 只能搬运国内跨平台素材（抖音/小红书/B站等），"
                f"禁止使用海外素材 [{sp}]（坚决禁止 IG 搬 IG 等海外互搬）",
            )
        return True, "合规（国内跨平台素材出海）"

    if is_domestic_target(tp):
        # 国内目标：只能搬海外素材
        if not is_overseas_source(sp):
            return (
                False,
                f"国内平台 [{tp}] 只能搬运海外优质素材（Instagram/YouTube/TikTok等），"
                f"禁止使用国内素材 [{sp}]（坚决禁止国内同台搬运/互搬）",
            )
        return True, "合规（海外优质素材进国内）"

    # 未知平台默认放行并告警
    return True, "未知平台放行"


def filter_valid_targets(source_platform: str, targets: list[str]) -> tuple[list[str], list[str]]:
    """批量过滤合法目标平台列表，返回 (合规目标列表, 违规被拦截目标列表)。"""
    valid: list[str] = []
    invalid: list[str] = []
    for t in targets:
        ok, _ = validate_cross_border(source_platform, t)
        if ok:
            valid.append(t)
        else:
            invalid.append(t)
    return valid, invalid

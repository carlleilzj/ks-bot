"""过滤链：对发现层产出的 Candidate 做筛选，决定是否入库审核。

规则：时长区间 + 关键词白名单/黑名单 + 热度阈值。
可选：对候选封面做真人检测（动画短视频应全是非真人，检测到真人则丢弃）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .discovery import Candidate

log = logging.getLogger(__name__)


@dataclass
class FilterRules:
    """过滤规则。从 config.yaml 的 discovery.filters 段加载。"""

    min_duration: float = 5
    max_duration: float = 120
    min_score: float = 0
    keyword_whitelist: list[str] | None = None   # None = 不限关键词
    keyword_blacklist: list[str] | None = None
    reject_real_person: bool = False   # 可选：真人检测反向过滤（动画号开启）

    @classmethod
    def from_dict(cls, d: dict | None) -> FilterRules:
        d = d or {}
        wl = d.get("keyword_whitelist")
        bl = d.get("keyword_blacklist")
        return cls(
            min_duration=float(d.get("min_duration", 5)),
            max_duration=float(d.get("max_duration", 120)),
            min_score=float(d.get("min_score", 0)),
            keyword_whitelist=[str(k).lower() for k in wl] if isinstance(wl, list) and wl else None,
            keyword_blacklist=[str(k).lower() for k in bl] if isinstance(bl, list) and bl else None,
            reject_real_person=bool(d.get("reject_real_person", False)),
        )


class FilterChain:
    """按顺序应用规则，返回 (是否保留, 原因)。"""

    def __init__(self, rules: FilterRules):
        self.rules = rules

    def keep(self, c: Candidate) -> tuple[bool, str]:
        r = self.rules
        # 时长：duration=0（RSS 不带时长）放行，只对有值的过滤
        if c.duration > 0 and not (r.min_duration <= c.duration <= r.max_duration):
            return False, f"时长 {int(c.duration)}s 不在区间 [{int(r.min_duration)},{int(r.max_duration)}]"
        # 热度：score<=0（RSS 用负数序数表示新旧，非热度值）跳过阈值检查
        if c.score > 0 and c.score < r.min_score:
            return False, f"热度 {c.score} < {r.min_score}"
        # 关键词命中检测（标题 + 来源说明 + 上传者）
        text = f"{c.title} {c.reason} {c.uploader}".lower()
        if r.keyword_blacklist:
            for k in r.keyword_blacklist:
                if k and k in text:
                    return False, f"命中黑名单关键词: {k}"
        # 白名单：可信来源（人工筛选的官方频道 RSS）跳过——标题常不含 anime 字样
        if r.keyword_whitelist and not c.trusted_source:
            if not any(k and k in text for k in r.keyword_whitelist):
                return False, "未命中白名单关键词"
        return True, "通过"

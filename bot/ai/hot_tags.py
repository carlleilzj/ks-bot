"""热门话题榜单：从回流播放量里动态算出 Top-N，注入文案 prompt。

闭环全景（2026-09-19）

    发布 → 定期抓播放量（stats.persist）→ tasks.play_count 落库
         → 本模块算 Top-N 话题榜
         → copywriter 生成时把榜单注入 prompt
         → AI 参考真实数据选话题 → 新作品发布 → （循环）

榜单 SQL：只统计高播放（默认 >=500）作品的话题，按「该话题下最高播放」
降序取前 10。之所以按最高播放而不是平均：
    一个话题被 1 条爆款用过（哪怕其他都是哑弹），也证明它进了正确的流量池；
    平均会被低分样本拉平，把真信号洗掉。

容错：榜单为空（新库/没抓过数据）时返回空列表，prompt 注入处自然跳过，
绝不阻塞生成 —— 数据回流是增强，不是依赖。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# 同一播放量分块内的解析：tags 字段可能是 JSON 数组，也可能是别的脏格式
_JSON_RE = re.compile(r"^\[.*\]$", re.S)


def parse_tags_field(raw: str | None) -> list[str]:
    """tasks.tags 字段 → 干净的标签列表。

    存量格式不统一（JSON 数组 / 空格分隔 / 带井号），这里统一处理：
    先试 JSON，失败按空白切分再剥 #。
    """
    if not raw:
        return []
    raw = raw.strip()
    if _JSON_RE.match(raw):
        try:
            j = json.loads(raw)
            if isinstance(j, list):
                return [str(t).lstrip("#").strip() for t in j if str(t).strip()]
        except json.JSONDecodeError:
            pass
    out: list[str] = []
    for piece in re.split(r"[\s,，]+", raw):
        t = piece.strip().lstrip("#").strip()
        if t:
            out.append(t)
    return out


def top_tags_from_rows(rows: list, min_play: int = 500, limit: int = 10,
                       exclude: set[str] | None = None) -> list[tuple[str, int, int]]:
    """从 task 行里聚合话题榜单。

    参数
        rows     —— 含 tags/play_count 的行（sqlite3.Row 或 dict）
        min_play —— 只统计播放量 >= 该值的作品（过滤哑弹噪声）
        limit    —— 最多返回几个话题
        exclude  —— 强制排除的词（如固定基础标签「无声视频」，它不算发现）

    返回 [(tag, 用过几次, 该 tag 最高播放)]，按最高播放降序。
    """
    exclude = exclude or set()
    agg: dict[str, list[int]] = {}
    for r in rows:
        row = dict(r) if not isinstance(r, dict) else r
        play = row.get("play_count")
        if not isinstance(play, int) or play < min_play:
            continue
        for t in parse_tags_field(row.get("tags")):
            if t in exclude or len(t) > 12:
                continue
            agg.setdefault(t, []).append(play)

    ranked = [(t, len(plays), max(plays)) for t, plays in agg.items()]
    ranked.sort(key=lambda x: (-x[2], -x[1]))
    return ranked[:limit]


def top_tags(db, min_play: int = 500, limit: int = 10,
             exclude: set[str] | None = None) -> list[tuple[str, int, int]]:
    """直接查库算 Top-N 话题榜（供 copywriter 调用）。"""
    try:
        with db._lock:
            rows = db.conn.execute(
                """SELECT tags, play_count FROM tasks
                   WHERE play_count IS NOT NULL
                     AND tags IS NOT NULL AND tags != ''"""
            ).fetchall()
    except Exception as e:
        log.warning("话题榜查询失败：%s", str(e)[:120])
        return []
    return top_tags_from_rows(rows, min_play=min_play, limit=limit,
                              exclude=exclude)


def format_for_prompt(ranked: list[tuple[str, int, int]]) -> str:
    """把榜单排成 prompt 里的紧凑段落。空榜单返回空串。"""
    if not ranked:
        return ""
    lines = []
    for i, (tag, n, top) in enumerate(ranked, 1):
        lines.append(f"{i}. #{tag}（实测最高播放 {top}，用过 {n} 次）")
    return "\n".join(lines)

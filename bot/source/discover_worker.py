"""国内发现 worker（阿里云侧）：用 B站 搜索为出海赛道找素材，推给协调端入库。

为什么单独一个进程
------------------
B站 搜索接口有**线路风控**：实测 2026-11-12，
  · 协调端 gc-jp（日本）请求 `api.bilibili.com/x/web-interface/search/type` → HTTP 412
  · 发布端 aliyun-gz（国内）同一接口 → HTTP 200 + code=0
而且连续请求也会 412，间隔 3~5 秒后恢复。

所以发现只能在阿里云跑，而任务库和下载/转码/文案都在协调端。
本进程只做「搜索 → 过滤 → POST /api/submit_url」，
下载/转码/文案/发布完全复用协调端既有流水线（与 TG 投链同一条路径）。

职责边界（很重要）
------------------
* **不**在这里下载视频、不调 LLM、不碰数据库 —— 只产链接。
* 过滤在本地做完（关键词/时长/画质），避免把垃圾链接推过去占协调端配额。
* 失败只影响自己：单个词搜不到就是没素材，不能让发布主流程跟着挂。

用法
----
    python -m bot.source.discover_worker            # 常驻（每 interval 分钟一轮）
    python -m bot.source.discover_worker --once     # 跑一轮就退出（调试）
    python -m bot.source.discover_worker --dry-run  # 只搜索并打印，不推送
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass, field

import httpx

from ..db import JobState

log = logging.getLogger("discover_worker")

# 单轮内两次 B站 搜索之间的最小间隔（秒）。
# 实测连续请求会 412，间隔 3~5 秒可恢复；取 5 秒留余量。
SEARCH_INTERVAL = 5.0

# 单轮每个赛道最多推送多少条（防止一次刷爆协调端待发队列）
MAX_PER_LANE = 4


@dataclass
class LanePlan:
    """一个出海赛道的寻源计划（从 config.yaml 的 platforms.<name>.discover 推导）。"""

    name: str                      # 发布目标，如 ig_anim
    queries: list[str] = field(default_factory=list)
    require_any: list[str] = field(default_factory=list)
    block_any: list[str] = field(default_factory=list)
    min_duration: float = 15.0
    max_duration: float = 180.0
    per_run: int = 4
    min_likes: int = 0

    @property
    def targets(self) -> list[str]:
        return [self.name]


def _strs(v) -> list[str]:
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def _num(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def lane_plans(s) -> list[LanePlan]:
    """从 Settings 里挑出「出海 + discover.enabled」的赛道，转成寻源计划。

    出海平台判定复用 cross_border.is_overseas_target —— 与入库时的方向校验
    用同一个判据，避免两处定义漂移。
    """
    from .cross_border import is_overseas_target

    plans: list[LanePlan] = []
    for name, plat in (s.platforms or {}).items():
        if not getattr(plat, "enabled", False):
            continue
        if not is_overseas_target(name):
            continue
        conf = getattr(plat, "discover", None) or {}
        if not conf.get("enabled"):
            continue
        if "bilibili" not in (conf.get("sources") or ["bilibili"]):
            continue
        queries = _strs(conf.get("queries"))
        if not queries:
            log.debug("[%s] 未配置 queries，跳过", name)
            continue
        plans.append(LanePlan(
            name=name,
            queries=queries,
            require_any=_strs(conf.get("require_any")),
            block_any=_strs(conf.get("block_any")),
            min_duration=_num(conf.get("min_duration"), 15.0),
            max_duration=_num(conf.get("max_duration"), 180.0),
            per_run=int(_num(conf.get("per_run"), MAX_PER_LANE)),
            min_likes=int(_num(conf.get("min_likes"), 0)),
        ))
    return plans


def _match(text: str, keywords: list[str]) -> list[str]:
    low = (text or "").lower()
    return [k for k in (keywords or []) if isinstance(k, str) and k and k.lower() in low]


def filter_candidate(cand, plan: LanePlan) -> tuple[bool, str]:
    """本地过滤：时长 → 排除词 → 必含词。

    顺序有讲究：**先时长后关键词**。B站 关键词召回噪声极大
    （「AI动画」返回 204 分钟教程课），先用时长一刀切掉长视频最省事。
    """
    if cand.duration and not (plan.min_duration <= cand.duration <= plan.max_duration):
        return False, (f"时长 {cand.duration:.0f}s 不在 "
                       f"{plan.min_duration:.0f}~{plan.max_duration:.0f}s")
    text = f"{cand.title} {cand.description}"
    blocked = _match(text, plan.block_any)
    if blocked:
        return False, f"命中排除词：{','.join(blocked)}"
    if plan.require_any:
        hit = _match(text, plan.require_any)
        if not hit:
            return False, "未命中赛道关键词"
    return True, "通过"


def collect(plan: LanePlan, *, dry_run: bool = False) -> list[dict]:
    """跑一个赛道的所有搜索词，返回通过过滤的候选（已去重、已截断）。"""
    from .discover import discover_bilibili

    picked: list[dict] = []
    seen: set[str] = set()
    for i, q in enumerate(plan.queries):
        if i:
            time.sleep(SEARCH_INTERVAL)
        cands = discover_bilibili(q, limit=20)
        if not cands:
            log.info("[%s] 搜索「%s」无结果（可能触发风控，下轮再试）", plan.name, q)
            continue
        kept = 0
        for c in cands:
            if c.url in seen:
                continue
            ok, why = filter_candidate(c, plan)
            if not ok:
                log.debug("[%s] 丢弃 %s：%s", plan.name, c.title[:30], why)
                continue
            seen.add(c.url)
            picked.append({
                "url": c.url,
                "title": c.title,
                "channel": c.channel,
                "platform": "bilibili",
                "duration": c.duration,
            })
            kept += 1
            if len(picked) >= plan.per_run:
                break
        log.info("[%s] 搜索「%s」：命中 %d 条（保留 %d）", plan.name, q, len(cands), kept)
        if len(picked) >= plan.per_run:
            break
    return picked[:plan.per_run]


def push(base: str, token: str, plan: LanePlan, items: list[dict]) -> dict:
    """把候选推给协调端入库。失败返回 {"ok": False}，不抛。"""
    if not items:
        return {"ok": True, "added": 0, "duplicate": 0}
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{base.rstrip('/')}/api/submit_url",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"targets": plan.targets, "items": items})
        if r.status_code != 200:
            log.warning("[%s] 推送失败 HTTP %d：%s", plan.name, r.status_code, r.text[:150])
            return {"ok": False}
        body = r.json()
        if body.get("rejected"):
            log.warning("[%s] 协调端拒绝 %d 条：%s",
                        plan.name, len(body["rejected"]), body["rejected"][:2])
        return body
    except Exception as e:
        log.warning("[%s] 推送异常：%s", plan.name, str(e)[:150])
        return {"ok": False}


def overseas_backlog(db, targets: list[str]) -> int:
    """出海专属待发水位：只数这些目标平台的 PENDING job。

    为什么不能直接用 db.backlog_size()：那是**全局**水位（国内快手积压占大头）。
    国内一顶到 24，出海寻源会被连带关掉 —— 而两者产能完全无关
    （国内 12 条/天，出海 8 条/天）。
    """
    if not targets:
        return 0
    ph = ",".join("?" * len(targets))
    try:
        with db._lock:
            row = db.conn.execute(
                f"SELECT COUNT(*) c FROM publish_jobs "
                f"WHERE state=? AND platform IN ({ph})",
                (JobState.PENDING, *targets),
            ).fetchone()
        return int(row["c"] or 0)
    except Exception as e:
        log.debug("读取出海水位失败（继续）：%s", e)
        return 0


def run_once(s, *, dry_run: bool = False, db=None) -> dict:
    """跑一轮所有出海赛道。返回 {赛道: {searched, pushed, added}}。

    db 不为空时做水位闸门：出海水位达到 discovery.backlog_watermark_overseas
    就整轮跳过。它必须独立于全局水位 —— 否则国内积压会把出海一起闷死。
    """
    base = (s.remote_api_url or "").strip()
    token = (s.remote_api_token or "").strip()
    if not dry_run and (not base or not token):
        log.error("REMOTE_API_URL / REMOTE_API_TOKEN 未配置，无法推送")
        return {}

    plans = lane_plans(s)
    if not plans:
        log.info("没有启用中的出海寻源赛道（检查 platforms.<name>.discover）")
        return {}

    if db is not None and not dry_run:
        conf = getattr(s, "discovery", None) or {}
        try:
            watermark = int(conf.get("backlog_watermark_overseas", 8) or 0)
        except (TypeError, ValueError):
            watermark = 8
        if watermark > 0:
            all_targets = [p.name for p in plans]
            backlog = overseas_backlog(db, all_targets)
            if backlog >= watermark:
                log.info("出海待发积压 %d 条已达水位 %d，本轮寻源跳过", backlog, watermark)
                return {}

    summary: dict[str, dict] = {}
    for plan in plans:
        try:
            items = collect(plan, dry_run=dry_run)
        except Exception as e:
            log.exception("[%s] 搜索异常（跳过该赛道）", plan.name)
            summary[plan.name] = {"error": str(e)[:100]}
            continue
        if dry_run:
            log.info("[%s] DRY-RUN：命中 %d 条", plan.name, len(items))
            for it in items:
                log.info("    %s  %s", it["url"], it["title"][:50])
            summary[plan.name] = {"found": len(items), "pushed": 0}
            continue
        res = push(base, token, plan, items)
        summary[plan.name] = {"found": len(items),
                              "pushed": len(items) if res.get("ok") else 0,
                              "added": res.get("added", 0)}
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m bot.source.discover_worker",
        description="国内发现 worker（B站 寻源 → 推送协调端入库）")
    parser.add_argument("--once", action="store_true", help="跑一轮就退出（调试）")
    parser.add_argument("--dry-run", action="store_true", help="只搜索并打印，不推送")
    parser.add_argument("--interval", type=int, default=0,
                        help="轮询间隔秒数（默认取 config 里最小 interval_hours）")
    args = parser.parse_args()

    from ..config import load_settings
    from ..db import Database
    from ..main import setup_logging

    setup_logging()
    s = load_settings()
    db = Database()   # 只为读出海水位（只读），任务仍由协调端管

    interval = args.interval or int(
        min([float((p.discover or {}).get("interval_hours", 6))
             for p in (s.platforms or {}).values()
             if (p.discover or {}).get("enabled")] or [6]) * 3600)

    log.info("国内发现 worker 启动：赛道 %s，间隔 %ds%s",
             [p.name for p in lane_plans(s)], interval,
             "（DRY-RUN）" if args.dry_run else "")

    while True:
        try:
            summary = run_once(s, dry_run=args.dry_run, db=db)
            if summary:
                log.info("本轮汇总：%s", json.dumps(summary, ensure_ascii=False))
        except Exception:
            log.exception("发现轮次异常（继续）")
        if args.once:
            break
        time.sleep(max(300, interval))


if __name__ == "__main__":
    main()

"""星火任务池变化跟踪。

背景（2026-10-15）：
星火「关联变现任务」的候选来自账号在快手 App 里收藏的任务，收藏动作
只能在 App 完成（PC 创作者中心无收藏入口，活动中心接口带 __NS_sig3
签名校验，无法程序化调用）。所以自动收藏做不到。

但任务池会变：新任务上架、老任务下架/停结算。
  实测：狐缘山间（0.285 元/条，最优）被厂商下架；
        山猎诡事、千万际遇辨亲情停结算；
        榆树沟出了个辣媳妇1 于 2026-09-10 新上架。
人工盯不过来，所以本模块在每次发布挂载时记录候选快照，
发现新增/消失就推 TG 提醒——你收到后去 App 收藏新任务即可。

状态文件：data/ks_spark_pool.json
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR

log = logging.getLogger(__name__)

POOL_PATH = DATA_DIR / "ks_spark_pool.json"


def _load(path: Path | None = None) -> dict:
    try:
        return json.loads((path or POOL_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict, path: Path | None = None) -> None:
    p = path or POOL_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except Exception as e:
        log.debug("任务池状态落盘失败：%s", e)


def _short(title: str) -> str:
    """标题去掉「任务时间：...」后缀，便于对比和展示。"""
    return title.split("任务时间")[0].strip() or title.strip()


def note_candidates(titles: list[str], notify=None,
                    pool_path: Path | None = None) -> dict:
    """记录本次候选任务快照，返回变化信息。

    titles: 发布页下拉里的候选（含「任务时间」后缀）
    notify: 可选的回调 notify(text)，用于推 TG（不传则只记日志）
    pool_path: 任务池缓存文件（多号各自一份，默认 A 号）

    返回 {"added": [...], "removed": [...], "first_run": bool}
    """
    now = [t.strip() for t in titles if t and t.strip()]
    if not now:
        return {"added": [], "removed": [], "first_run": False}

    cur = {_short(t): t for t in now}
    data = _load(pool_path)
    prev = data.get("tasks") or {}
    first_run = not prev

    added = [k for k in cur if k not in prev]
    removed = [k for k in prev if k not in cur]

    data["tasks"] = cur
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _save(data, pool_path)

    if first_run:
        log.info("星火任务池首次记录：%d 个任务", len(cur))
        return {"added": [], "removed": [], "first_run": True}

    if added:
        log.info("星火任务池新增 %d 个：%s", len(added), added)
    if removed:
        log.info("星火任务池移除 %d 个：%s", len(removed), removed)

    if notify and (added or removed):
        lines = ["🔔 星火任务池有变化"]
        if added:
            lines.append("")
            lines.append(f"➕ 新增 {len(added)} 个（去 App 收藏才能挂）：")
            for k in added:
                lines.append(f"  · {k}")
        if removed:
            lines.append("")
            lines.append(f"➖ 消失 {len(removed)} 个：")
            for k in removed:
                lines.append(f"  · {k}")
        lines.append("")
        lines.append("收藏后系统会自动开始挂载并统计收益。")
        try:
            notify("\n".join(lines))
        except Exception as e:
            log.debug("任务池变化通知失败：%s", e)

    return {"added": added, "removed": removed, "first_run": False}


def current_pool() -> dict:
    """当前记录的任务池（供报告/调试用）。"""
    return (_load().get("tasks") or {})

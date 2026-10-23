"""快手星火收益抓取与归因。

背景（2026-10-15）：
- 账号已挂星火变现任务 162 次，累计收益 58.94 元、可提现 78.79 元，
  平均每次挂载约 0.36 元 —— 不是"没变现"，是变现效率太低。
- 星火收益按**任务维度**结算（快手账单里显示为「星火计划达人结算-XX」），
  而发布端此前只写日志不落库，无法回答"哪个任务更赚钱"。
- 本模块拉取创作者中心收益汇总，按时间窗口累积到本地，
  配合 publish_jobs.spark_task 就能算出各任务的单条产出。

数据来源（PC 创作者中心，需登录态）：
  POST https://cp.kuaishou.com/rest/cp/creator/pc/home/income
  → {"income": 58.94, "banance": 78.79, "enableTaxReform": true}
  注：接口字段名就是 "banance"（平台的拼写错误），不是 balance。

用法：
  python -m bot.monitor.spark_income            # 抓一次并落库
  python -m bot.monitor.spark_income --report   # 只输出归因报告
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR, KS_STATE_PATH

log = logging.getLogger(__name__)

INCOME_URL = "https://cp.kuaishou.com/rest/cp/creator/pc/home/income"
HOME_URL = "https://cp.kuaishou.com/profile"
SNAPSHOT_PATH = DATA_DIR / "ks_income_snapshots.json"


def _load_snapshots(path: Path | None = None) -> list[dict]:
    try:
        return json.loads((path or SNAPSHOT_PATH).read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_snapshots(rows: list[dict], path: Path | None = None) -> None:
    p = path or SNAPSHOT_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # 只保留最近 400 条，避免无限增长
        p.write_text(
            json.dumps(rows[-400:], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log.warning("收益快照落盘失败：%s", e)


def fetch_income(headless: bool = True,
                 state_path: Path | str | None = None) -> dict | None:
    """打开发布页拿收益汇总。返回 {"income":float,"balance":float} 或 None。

    state_path：多号时传对应登录态（默认 A 号 KS_STATE_PATH）。
    收益接口在创作者中心首页触发，因此先开首页再等待 XHR。
    """
    from playwright.sync_api import sync_playwright
    from ..publish.base import launch_chromium, new_context

    sp = Path(state_path or KS_STATE_PATH)
    if not sp.exists():
        log.warning("快手登录态不存在（%s），跳过收益抓取", sp.name)
        return None

    result: dict = {}

    with sync_playwright() as pw:
        browser = launch_chromium(pw, headless=headless)
        context = new_context(browser, sp)
        page = context.new_page()

        def on_resp(r):
            if "home/income" in r.url:
                try:
                    d = r.json().get("data") or {}
                    result["income"] = float(d.get("income") or 0)
                    # 平台字段名就是 banance（拼写错误），兼容 balance
                    result["balance"] = float(d.get("banance") or d.get("balance") or 0)
                except Exception:
                    pass

        page.on("response", on_resp)
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(10000)
            # 触发一次刷新，确保拿到最新值
            if not result:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(8000)
                except Exception:
                    pass
        except Exception as e:
            log.warning("收益抓取页面异常：%s", str(e)[:150])
        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass

    if not result:
        log.warning("收益接口未捕获到数据（登录态可能已失效）")
        return None
    return result


def record(headless: bool = True, state_path: Path | str | None = None,
           snapshot_path: Path | str | None = None) -> dict | None:
    """抓一次收益并追加快照。返回本次快照或 None。

    state_path / snapshot_path：多号各自一份（默认 A 号路径不变）。
    """
    got = fetch_income(headless=headless, state_path=state_path)
    if not got:
        return None
    snap = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "income": got["income"],
        "balance": got["balance"],
    }
    sp = Path(snapshot_path) if snapshot_path else SNAPSHOT_PATH
    rows = _load_snapshots(sp)
    rows.append(snap)
    _save_snapshots(rows, sp)
    log.info("收益快照已记录（%s）：累计 %.2f 元，可提现 %.2f 元",
             sp.name, snap["income"], snap["balance"])
    return snap


def _delta_since(days: int) -> float | None:
    """最近 N 天的收益增量（用快照差值估算）。"""
    rows = _load_snapshots()
    if len(rows) < 2:
        return None
    cutoff = datetime.now().timestamp() - days * 86400
    older = [r for r in rows
             if datetime.fromisoformat(r["at"]).timestamp() <= cutoff]
    base = older[-1] if older else rows[0]
    return round(rows[-1]["income"] - base["income"], 2)


def report(days: int = 30) -> str:
    """输出收益归因报告：总收益 + 各任务发布量 + 均摊单条产出。"""
    from ..db import Database

    db = Database()
    stats = db.spark_task_stats(days=days)
    recent = db.recent_kuaishou_spark(limit=15)

    rows = _load_snapshots()
    cur_income = rows[-1]["income"] if rows else None
    cur_balance = rows[-1]["balance"] if rows else None
    delta = _delta_since(days)

    lines = []
    lines.append(f"=== 星火收益归因（近 {days} 天）===")
    if cur_income is not None:
        lines.append(f"累计收益：{cur_income:.2f} 元｜可提现：{cur_balance:.2f} 元")
    if delta is not None:
        lines.append(f"近 {days} 天增量：{delta:.2f} 元")
    lines.append("")

    if not stats:
        lines.append("尚无 spark_task 记录（本轮改动上线后发布的作品才会记录）")
    else:
        total_pub = sum(r["published"] for r in stats)
        lines.append(f"{'任务':<28}{'发布数':>6}{'均摊单条':>10}")
        lines.append("-" * 46)
        for r in stats:
            name = (r["spark_task"] or "")[:26]
            # 均摊：用近 N 天增量按发布量平均（样本足够后才有参考价值）
            per = (delta / total_pub) if (delta and total_pub) else 0
            lines.append(f"{name:<28}{r['published']:>6}{per:>9.3f}元")
        lines.append("")
        lines.append(f"合计发布：{total_pub} 条")
        lines.append("说明：单条产出为均摊估算。快手账单按任务维度结算，"
                     "逐条精确归因需累计至少一周数据。")

    if recent:
        lines.append("")
        lines.append("最近挂载记录：")
        for r in recent[:10]:
            lines.append(f"  {r['published_at']}  {(r['spark_task'] or '')[:24]}  "
                         f"[{r['shortcode']}]")

    text = "\n".join(lines)
    print(text)
    return text


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m bot.monitor.spark_income")
    ap.add_argument("--report", action="store_true", help="只输出归因报告，不抓取")
    ap.add_argument("--days", type=int, default=30, help="报告统计天数")
    ap.add_argument("--headed", action="store_true", help="有头模式（调试）")
    args = ap.parse_args()

    from ..main import setup_logging
    setup_logging()

    if not args.report:
        record(headless=not args.headed)
    report(days=args.days)


if __name__ == "__main__":
    main()

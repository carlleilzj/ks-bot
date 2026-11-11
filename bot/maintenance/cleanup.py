"""定期清理中间文件和旧日志，回收磁盘空间。

清理策略：
- 已通知（NOTIFIED）终态任务：删除 raw + work 中间文件，final 保留 keep_final_days 天后删除
- 日志调试截图（*.png）：超过 keep_screenshot_days 天的删除
- 日志文件（bot.log / launchd.*.log）：超过 max_log_mb 则轮转（截断保留尾部）
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..config import FINAL_DIR, LOGS_DIR, RAW_DIR, WORK_DIR
from ..db import Database, State

log = logging.getLogger(__name__)

# final 成品保留天数（发布后 7 天，确认无投诉再删）
KEEP_FINAL_DAYS = 7
# 调试截图保留天数
KEEP_SCREENSHOT_DAYS = 3
# 日志文件大小上限（MB），超过则截断尾部 50%
MAX_LOG_MB = 10
# READY 任务最长排队天数：超过仍未发出 → 标 SKIPPED 并删媒体。
# 背景（2026-11-12）：产能 12 条/天 而发现层约 21-31 条/天，READY 一路涨到
# 32 条，最老的等了 2 天。素材有保质期，两周前的链接发出去也错过热点，
# 留着只占水位名额。3 天 = 约两轮产能周期，足够容忍短暂堵塞。
KEEP_READY_DAYS = 3


def _file_age_days(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 86400


def _truncate_log(path: Path, max_mb: int) -> None:
    """日志文件超过 max_mb 则保留尾部一半内容。"""
    size = path.stat().st_size
    if size <= max_mb * 1024 * 1024:
        return
    keep_bytes = size // 2
    with open(path, "rb") as f:
        f.seek(-keep_bytes, 2)
        data = f.read()
    path.write_bytes(data)
    log.info("日志轮转 %s（%dMB → %dMB）", path.name, size // 1048576, len(data) // 1048576)


_MEDIA_SUFFIXES = (
    "_final.mp4", "_tc.mp4", "_export.mp4", "_cover.jpg", "_copy.json", ".mp4", ".srt", ".ass",
)


def _shortcode_from_name(name: str) -> str:
    for suffix in _MEDIA_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def purge_task_media(task: dict, *, keep_final: bool = False) -> dict:
    """删除一条任务的 raw/work，可选保留 final。"""
    stats = {"raw_deleted": 0, "work_deleted": 0, "final_deleted": 0, "freed_mb": 0}
    sc = task.get("shortcode") or ""

    raw = Path(task["raw_path"]) if task.get("raw_path") else RAW_DIR / f"{sc}.mp4"
    if raw.exists():
        stats["freed_mb"] += raw.stat().st_size // 1048576
        raw.unlink()
        stats["raw_deleted"] += 1

    for f in WORK_DIR.glob(f"{sc}*"):
        if f.exists():
            stats["freed_mb"] += f.stat().st_size // 1048576
            f.unlink()
            stats["work_deleted"] += 1

    if not keep_final:
        final = Path(task["final_path"]) if task.get("final_path") else FINAL_DIR / f"{sc}_final.mp4"
        if final.exists():
            stats["freed_mb"] += final.stat().st_size // 1048576
            final.unlink()
            stats["final_deleted"] += 1
        extra = FINAL_DIR / f"{sc}_final.mp4"
        if extra.exists() and extra != final:
            extra.unlink()
            stats["final_deleted"] += 1
    return stats


def cleanup_media(db: Database) -> dict:
    """清理已终态任务的媒体文件，返回统计。"""
    stats = {"raw_deleted": 0, "work_deleted": 0, "final_deleted": 0,
             "work_freed_mb": 0, "final_freed_mb": 0}

    for r in db.cleanup_candidates():
        sc = r["shortcode"]
        drop_final_now = r["state"] == State.SKIPPED
        one = purge_task_media(r, keep_final=not drop_final_now)
        stats["raw_deleted"] += one["raw_deleted"]
        stats["work_deleted"] += one["work_deleted"]
        stats["work_freed_mb"] += one["freed_mb"]
        if drop_final_now:
            stats["final_deleted"] += one["final_deleted"]
            continue
        final = Path(r["final_path"]) if r.get("final_path") else FINAL_DIR / f"{sc}_final.mp4"
        if final.exists() and _file_age_days(final) > KEEP_FINAL_DAYS:
            sz = final.stat().st_size
            final.unlink()
            stats["final_deleted"] += 1
            stats["final_freed_mb"] += sz // 1048576

    return stats


def purge_orphans(db: Database) -> dict:
    """删除 media/ 里 DB 不再保留的孤儿文件。"""
    keep = db.keep_media_shortcodes()
    stats = {"deleted": 0, "freed_mb": 0}
    for folder in (RAW_DIR, WORK_DIR, FINAL_DIR):
        if not folder.exists():
            continue
        for f in folder.iterdir():
            if not f.is_file():
                continue
            sc = _shortcode_from_name(f.name)
            if sc in keep:
                continue
            try:
                stats["freed_mb"] += f.stat().st_size // 1048576
                f.unlink()
                stats["deleted"] += 1
            except OSError:
                pass
    return stats


def cleanup_screenshots() -> int:
    """清理过期调试截图，返回删除数量。"""
    count = 0
    for f in LOGS_DIR.glob("*.png"):
        if f.exists() and _file_age_days(f) > KEEP_SCREENSHOT_DAYS:
            f.unlink()
            count += 1
    return count


def rotate_logs() -> None:
    """轮转超大日志文件。"""
    for name in ("bot.log", "launchd.err.log", "launchd.out.log"):
        p = LOGS_DIR / name
        if p.exists():
            _truncate_log(p, MAX_LOG_MB)


def expire_stale_ready(db: Database, days: float = KEEP_READY_DAYS) -> dict:
    """把排队超期的 READY 任务标 SKIPPED 并删媒体，返回统计。

    为什么单独一件事而不是塞进 cleanup_media：cleanup_media 只处理**终态**
    任务（NOTIFIED/FAILED/SKIPPED），READY 是在制品，属于状态机活的任务。
    混在一起会让「清理旧文件」意外改动流水线状态，不好排查。

    为什么删媒体：这些 final 不会再发，留着只会让 purge_orphans 每次都
    重新扫一遍、白占磁盘（实测一条约 12.8MB，积压 30 条就是 380MB）。
    """
    stats = {"expired": 0, "freed_mb": 0, "shortcodes": []}
    for task in db.expired_ready(days):
        sc = task.get("shortcode") or ""
        try:
            one = purge_task_media(task, keep_final=False)
            stats["freed_mb"] += one.get("freed_mb", 0)
            # purge_task_media 的 freed_mb 不含 final，单独补上
            final = (Path(task["final_path"]) if task.get("final_path")
                     else FINAL_DIR / f"{sc}_final.mp4")
            if final.exists():
                stats["freed_mb"] += final.stat().st_size // 1048576
                final.unlink(missing_ok=True)
            # 必须同时把待发 job 收成终态：否则任务虽 SKIPPED，PENDING job 仍在
            # backlog_size() 里被统计，水位永远不会降下来 → 闸门永久关闭、发现层
            # 再也恢复不了（本地测试实测：只改 task 时 backlog 仍为 3）。
            for job in db.pending_jobs(task["id"]):
                db.update_job(job["id"], state="SKIPPED",
                              error=f"source task expired: queued > {days:g} days")
            db.update(task["id"], state=State.SKIPPED,
                      error=f"排队超过 {days:g} 天未发出（素材时效性过期），已自动清理")
            stats["expired"] += 1
            stats["shortcodes"].append(sc)
            log.info("[%s] READY 排队超 %g 天，已标 SKIPPED 并删媒体", sc, days)
        except Exception:
            log.exception("[%s] 过期清理失败（跳过该条）", sc)
    return stats


def run_cleanup(db: Database) -> None:
    """执行完整清理流程。"""
    log.info("开始清理旧文件...")

    # 先处理排队超期的 READY：它们会释放水位名额，让发现层尽快恢复。
    try:
        expired = expire_stale_ready(db)
        if expired["expired"]:
            log.warning("READY 超期清理：%d 条（%s），释放 %dMB",
                        expired["expired"], ", ".join(expired["shortcodes"][:5]),
                        expired["freed_mb"])
    except Exception:
        log.exception("READY 超期清理失败（不影响其他清理）")

    media = cleanup_media(db)
    if media["raw_deleted"] or media["work_deleted"] or media["final_deleted"]:
        log.info(
            "媒体清理：raw %d 个、work %d 个、final %d 个；释放 %dMB",
            media["raw_deleted"], media["work_deleted"], media["final_deleted"],
            media["work_freed_mb"] + media["final_freed_mb"],
        )

    shots = cleanup_screenshots()
    if shots:
        log.info("清理调试截图 %d 张", shots)

    orphans = purge_orphans(db)
    if orphans["deleted"]:
        log.info("孤儿媒体 %d 个，释放 %dMB", orphans["deleted"], orphans["freed_mb"])

    rotate_logs()
    log.info("清理完成")

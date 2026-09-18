"""远程发布 worker（家庭端）：轮询 VPS Remote API，认领任务并在本地发布。

部署在家庭旧电脑/ publish 机器上（住宅 IP，国内平台风控友好）。
依赖：Playwright + 各平台登录态（data/*_state.json）+ REMOTE_API_URL/TOKEN。

流程：
  循环轮询 /api/pending
  → 对每个 gate=None 的平台 job：POST /api/claim 原子认领
  → 下载 final/cover 文件（sha256 校验）
  → 本地 publish_gate（窗口已由 VPS 判断，这里只留平台间隔兜底）
  → Playwright 发布
  → POST /api/report 回报（成功/失败/登录失效）

用法：python -m bot.publish_worker [--once] [--interval 120]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import time
from pathlib import Path

import httpx

from .config import MEDIA_DIR, Settings, load_settings
from .notify import telegram
from .audit_check import _parse_dt

log = logging.getLogger("publish_worker")

REMOTE_DIR = MEDIA_DIR / "remote"  # 家庭端下载缓存


class WorkerError(Exception):
    pass


def _headers(s: Settings) -> dict:
    return {"Authorization": f"Bearer {s.remote_api_token}"}


def _check(resp: httpx.Response, what: str) -> dict:
    if resp.status_code == 401:
        raise WorkerError(f"{what}: 认证失败（检查 REMOTE_API_TOKEN）")
    if resp.status_code != 200:
        raise WorkerError(f"{what}: HTTP {resp.status_code} {resp.text[:150]}")
    return resp.json()


def fetch_pending(base: str, s: Settings) -> list[dict]:
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{base}/api/pending", headers=_headers(s))
        return _check(r, "拉取待发布清单").get("tasks", [])


def claim(base: str, s: Settings, task_id: int, platform: str, force: bool = False) -> dict | None:
    """原子认领。返回 task payload；被别人抢先/状态不对/gate 拦截返回 None。"""
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{base}/api/claim", headers=_headers(s),
                   json={"task_id": task_id, "platform": platform, "force": force})
        data = _check(r, f"认领 {task_id}/{platform}")
    if not data.get("ok"):
        if data.get("gate_blocked"):
            log.info("[%s] %s gate 拦截：%s（下轮自动重试）",
                     task_id, platform, data.get("error", "")[:80])
        return None
    return data.get("task")


def report(base: str, s: Settings, task_id: int, platform: str, ok: bool,
           url: str = "", error: str = "", login_expired: bool = False) -> None:
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{base}/api/report", headers=_headers(s),
                   json={"task_id": task_id, "platform": platform, "ok": ok,
                         "url": url, "error": error, "login_expired": login_expired})
        _check(r, f"回报 {task_id}/{platform}")



def fetch_published(base: str, s: Settings, platform: str = "douyin",
                    days: int = 7) -> list[dict]:
    """拉 VPS 上近期已发布作品清单（审核巡检测匹配用）。"""
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{base}/api/published",
                  params={"platform": platform, "days": days}, headers=_headers(s))
        return _check(r, "拉取已发布清单").get("items", [])


def report_audit(base: str, s: Settings, task_id: int, note: str, deleted: bool) -> None:
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{base}/api/audit", headers=_headers(s),
                   json={"task_id": task_id, "note": note, "deleted": deleted})
        _check(r, f"回报审核 {task_id}")


def run_audit_check(base: str, s: Settings, dry_run: bool = False) -> int:
    """抖音审核违规巡检：发现违规通知 → 自动删除对应作品。返回处理数。"""
    from . import audit_check

    try:
        published = fetch_published(base, s, "douyin", days=7)
    except Exception as e:
        log.warning("拉取已发布清单失败：%s", str(e)[:120])
        return 0
    if not published:
        log.info("无近期抖音已发布作品，跳过审核巡检")
        return 0

    log.info("审核巡检开始（已发布 %d 条）", len(published))
    results = audit_check.run_audit(published, headless=s.publish.headless, dry_run=dry_run)
    handled = 0
    for r in results:
        if r.get("error"):
            log.error("审核巡检出错：%s", r["error"][:150])
            continue
        matched = r.get("matched")
        # 兼容两种来源：通知类结果带 'notice'，作品类结果带 'cand'。
        # 原来硬取 r['notice'] 会在作品类结果上抛 KeyError（实测踩到）。
        notice = r.get("notice") or {}
        cand = r.get("cand") or {}
        detail = (notice.get("text") or cand.get("reason")
                  or cand.get("title") or "")
        note = (f"标题：{(matched or {}).get('title', '')[:60]}｜"
                f"发布：{(matched or {}).get('published_at', '')}｜"
                f"原因：{detail[:200]}")
        if not matched:
            log.warning("违规项未匹配到作品，仅告警：%s", detail[:120])
            try:
                telegram.notify_info(s, f"⚠️ 抖音审核违规（未匹配到本地作品）\n"
                                        f"{detail[:300]}")
            except Exception:
                pass
            continue
        handled += 1
        sc = matched.get("shortcode", "")
        if r.get("dry_run"):
            log.info("[DRY] 将删除 %s（task %s）", sc, matched.get("task_id"))
            continue
        try:
            report_audit(base, s, matched["task_id"], note, bool(r.get("deleted")))
        except Exception as e:
            log.warning("回报审核结果失败：%s", str(e)[:120])
        if r.get("deleted"):
            log.warning("[%s] 审核违规，作品已删除：%s", sc, matched.get("title", "")[:40])
            try:
                telegram.notify_info(
                    s, f"🗑️ 抖音审核违规，已自动删除作品\n"
                       f"任务：{sc}\n标题：{matched.get('title', '')[:50]}\n"
                       f"原因：{r['notice'].get('text', '')[:200]}")
            except Exception:
                pass
        else:
            log.error("[%s] 审核违规，但删除失败（需人工处理）", sc)
            try:
                telegram.notify_info(
                    s, f"⚠️ 抖音审核违规，自动删除失败，请人工处理\n"
                       f"任务：{sc}\n标题：{matched.get('title', '')[:50]}")
            except Exception:
                pass
    return handled


def download_file(base: str, s: Settings, entry: dict, dest_dir: Path) -> Path:
    """下载文件 + sha256 校验；不匹配删除重试一次。"""
    dest = dest_dir / entry["name"]
    expected = entry.get("sha256", "")
    for attempt in (1, 2):
        if dest.exists() and expected and _sha256(dest) == expected and \
                dest.stat().st_size == entry.get("size", -1):
            return dest  # 上次已下载且校验过
        with httpx.Client(timeout=300, follow_redirects=True) as c:
            with c.stream("GET", f"{base}/api/file",
                          params={"path": entry["path"]}, headers=_headers(s)) as r:
                if r.status_code != 200:
                    raise WorkerError(f"下载 {entry['name']}: HTTP {r.status_code}")
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                tmp.replace(dest)
        actual = _sha256(dest)
        if not expected or actual == expected:
            return dest
        log.warning("下载校验失败（第 %d 次）：%s sha256 不匹配", attempt, dest.name)
        dest.unlink(missing_ok=True)
    raise WorkerError(f"文件校验连续失败：{entry['name']}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _platform_copy(task: dict, platform: str) -> dict:
    """优先用 copy_json 里的平台专属文案，退回任务级 title/description。"""
    data: dict = {}
    if task.get("copy_json"):
        try:
            data = json.loads(task["copy_json"])
        except Exception:
            data = {}
    copy = data.get(platform) or {}
    return {
        "title": copy.get("title") or task.get("title") or "",
        "description": copy.get("description") or task.get("description") or "",
        "tags": copy.get("tags") or task.get("tags") or [],
        "category": copy.get("category") or task.get("category") or None,
    }


def process_platform(base: str, s: Settings, task: dict, plat_info: dict, force: bool = False) -> None:
    """处理单个平台 job：认领 → 下载 → 发布 → 回报。异常就地回报失败，不中断其他平台。"""
    from .publish import get_publisher
    from .publish.base import LoginExpired
    from .publish.kuaishou import SparkAttachError

    task_id, platform = task["task_id"], plat_info["platform"]
    try:
        payload = claim(base, s, task_id, platform, force=force)
        if not payload:
            log.info("[%s] %s 认领失败（已被处理或状态变化），跳过",
                     task.get("shortcode"), platform)
            return
        pub = get_publisher(platform)
        if not pub.state_path.exists():
            report(base, s, task_id, platform, ok=False,
                   error="登录态文件不存在", login_expired=True)
            return

        dest_dir = REMOTE_DIR / str(task_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        video = download_file(base, s, payload["final_file"], dest_dir)
        cover = None
        if payload.get("cover_file"):
            try:
                cover = download_file(base, s, payload["cover_file"], dest_dir)
            except WorkerError as e:
                log.warning("[%s] 封面下载失败（无封面发布）：%s", task["shortcode"], e)

        copy = _platform_copy(payload, platform)
        extra = {}
        plat_cfg = s.platforms.get(platform)
        if platform == "kuaishou":
            if plat_cfg and plat_cfg.spark_task:
                extra["spark_task"] = True
                extra["spark_task_title"] = plat_cfg.spark_task_title
        elif platform == "douyin":
            # 挂载标签（商品/位置/小程序/团购/热点），配置驱动，失败不阻塞发布
            if plat_cfg and getattr(plat_cfg, "anchors", None):
                extra["anchors"] = dict(plat_cfg.anchors)
            if plat_cfg and getattr(plat_cfg, "hot_topic", ""):
                extra["hot_topic"] = plat_cfg.hot_topic
        log.info("[%s] %s 开始发布：%s", task["shortcode"], pub.display_name, copy["title"][:40])
        url = pub.publish(
            video=video,
            title=copy["title"],
            description=copy["description"],
            tags=copy["tags"],
            category=copy["category"],
            cover=cover,
            headless=s.publish.headless,
            state_path=pub.state_path,
            **extra,
        )
        report(base, s, task_id, platform, ok=True, url=url or "")
        log.info("[%s] %s 发布成功：%s", task["shortcode"], pub.display_name, url or "-")
        if platform == "douyin":
            schedule_douyin_audit(5 * 60, reason=f"发布后 5 分钟初审复检 [{task['shortcode']}]")
            schedule_douyin_audit(15 * 60, reason=f"发布后 15 分钟二审复检 [{task['shortcode']}]")
        # 发布完成后清理该任务缓存视频
        shutil.rmtree(dest_dir, ignore_errors=True)
    except LoginExpired as e:
        log.warning("[%s] %s 登录态失效：%s", task.get("shortcode"), platform, str(e)[:120])
        report(base, s, task_id, platform, ok=False, error=str(e)[:500], login_expired=True)
    except SparkAttachError as e:
        # 星火挂载未生效 → 阻断发布。这是配置/平台侧问题，重试通常无解，
        # 但也不该静默白发：回报失败让 job 回 PENDING，并 Telegram 告警，
        # 由人工决定（去 App 重新收藏任务 / 临时关掉 spark_task）。
        log.error("[%s] %s 星火挂载阻断发布：%s", task.get("shortcode"), platform, e)
        try:
            report(base, s, task_id, platform, ok=False,
                   error=f"星火挂载未生效，已阻断发布：{str(e)[:400]}")
        except Exception:
            log.error("回报星火阻断结果失败（网络断），下轮重试")
        try:
            telegram.notify_info(
                s,
                f"🚫 [{task.get('shortcode')}] {platform} 星火挂载未生效，已阻断发布\n"
                f"避免无收益白发。请到快手 App 星火计划确认任务是否仍可挂 / 额度是否已满，"
                f"或在 config.yaml 把 spark_task 设为 false 跳过挂载\n"
                f"失败截图：logs/ks_spark_verify_fail.png")
        except Exception:
            log.debug("星火阻断告警推送失败", exc_info=True)
    except WorkerError as e:
        # 网络/API 层错误：回报普通失败，VPS 会把 job 放回 PENDING
        log.error("[%s] %s worker 错误：%s", task.get("shortcode"), platform, e)
        try:
            report(base, s, task_id, platform, ok=False, error=str(e)[:500])
        except Exception:
            log.error("回报失败结果也失败（网络断），下轮重试")
    except Exception as e:
        log.exception("[%s] %s 发布异常", task.get("shortcode"), platform)
        try:
            report(base, s, task_id, platform, ok=False, error=str(e)[:500])
        except Exception:
            log.error("回报失败结果也失败（网络断），下轮重试")


# 审核巡检节流与发布追踪调度
_last_audit_at: float = 0.0
AUDIT_INTERVAL = 30 * 60  # 30 分钟兜底巡检
_scheduled_audits: list[float] = []


def schedule_douyin_audit(delay_seconds: int, reason: str = "") -> None:
    """预约在指定秒数后触发一次定向巡检（如发布后 5 分钟、15 分钟）。"""
    if delay_seconds <= 0:
        delay_seconds = 5
    target = time.time() + delay_seconds
    # 去重：若已有预约在目标时间 ±45 秒内，则不重复加入
    if any(abs(target - existing) < 45 for existing in _scheduled_audits):
        return
    _scheduled_audits.append(target)
    _scheduled_audits.sort()
    log.info("已预约抖音审核复检（%s）：将在 %d 秒后执行（预计 %s）",
             reason or "追踪",
             delay_seconds,
             time.strftime("%H:%M:%S", time.localtime(target)))


def maybe_audit(base: str, s: Settings) -> None:
    """按常规周期或发布追踪计划跑抖音审核巡检（失败不影响发布主流程）。"""
    global _last_audit_at, _scheduled_audits
    if not s.platforms.get("douyin") or not s.platforms["douyin"].enabled:
        return
    now = time.time()

    # 检查是否有到期的预约巡检
    due = [t for t in _scheduled_audits if now >= t]
    is_scheduled_due = bool(due)
    is_periodic_due = (now - _last_audit_at >= AUDIT_INTERVAL)

    if not is_scheduled_due and not is_periodic_due:
        return

    if is_scheduled_due:
        _scheduled_audits = [t for t in _scheduled_audits if now < t]
        log.info("触发发布后定向复检（剩余待复检任务：%d 个）", len(_scheduled_audits))
    else:
        gap_str = f"距上次 {(now - _last_audit_at) / 60:.1f} 分钟" if _last_audit_at > 0 else "初始启动"
        log.info("触发常规兜底巡检（%s）", gap_str)

    _last_audit_at = now
    try:
        run_audit_check(base, s)
    except Exception:
        log.exception("审核巡检异常（忽略，不影响发布）")


def run_once(base: str, s: Settings) -> int:
    """一轮：拉清单 → 逐个可发布平台处理 → 审核巡检。返回处理数。"""
    tasks = fetch_pending(base, s)
    n = 0
    for task in tasks:
        for plat in task.get("platforms", []):
            if plat.get("gate"):
                log.debug("[%s] %s gate 未放行：%s",
                          task.get("shortcode"), plat["platform"], plat["gate"])
                continue
            if plat.get("retries", 0) >= 5:
                log.warning("[%s] %s 重试已达 5 次，等人工处理", task.get("shortcode"), plat["platform"])
                continue
            process_platform(base, s, task, plat)
            n += 1
            time.sleep(5)  # 平台间小间隔
    maybe_audit(base, s)
    return n


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bot.publish_worker",
                                     description="远程发布 worker（家庭端）")
    parser.add_argument("--once", action="store_true", help="跑一轮就退出（调试）")
    parser.add_argument("--interval", type=int, default=120, help="轮询间隔秒数（默认 120）")
    parser.add_argument("--audit", action="store_true",
                        help="只跑一轮抖音审核巡检（违规通知→自动删除作品）后退出")
    parser.add_argument("--audit-dry-run", action="store_true",
                        help="配合 --audit：只匹配不删除")
    args = parser.parse_args()

    from .main import setup_logging
    setup_logging()
    s = load_settings()

    if not s.remote_api_url or not s.remote_api_token:
        log.error("REMOTE_API_URL / REMOTE_API_TOKEN 未配置（.env），无法连接 VPS")
        raise SystemExit(1)
    base = s.remote_api_url.rstrip("/")

    if args.audit:
        log.info("审核巡检模式（dry_run=%s）", args.audit_dry_run)
        n = run_audit_check(base, s, dry_run=args.audit_dry_run)
        log.info("审核巡检结束，处理 %d 条", n)
        return

    log.info("发布 worker 启动 → %s（间隔 %ds）", base, args.interval)
    try:
        telegram.notify_info(s, f"🟢 发布 worker 已启动\n远端：{base}\n间隔 {args.interval}s")
    except Exception:
        pass

    # 启动时释放卡死的 PUBLISHING job：上次 worker 若在发布中途被杀
    # （部署重启/崩溃），job 会永久停在 PUBLISHING，既不会被认领也不会回报。
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{base}/api/release_stale", headers=_headers(s),
                       json={"older_than_minutes": 30})
        released = (r.json() or {}).get("released", 0)
        if released:
            log.warning("启动清理：释放 %d 条卡死的发布中 job（已放回待发布队列）", released)
            try:
                telegram.notify_info(s, f"🧹 发布端启动清理：释放了 {released} 条卡死的发布中 job")
            except Exception:
                pass
    except Exception as e:
        log.debug("启动释放卡死 job 失败：%s", e)

    # 启动时恢复近期发布追踪：如果 20 分钟内有新发布抖音作品，自动补齐复检预约
    try:
        recent_pub = fetch_published(base, s, "douyin", days=1)
        now_ts = time.time()
        for item in recent_pub:
            dt = _parse_dt(item.get("published_at", ""))
            if dt:
                age = now_ts - dt.timestamp()
                title_short = item.get("title", "")[:12]
                if 0 <= age < 5 * 60:
                    schedule_douyin_audit(int(5 * 60 - age), reason=f"启动补齐 5m 复检 [{title_short}]")
                    schedule_douyin_audit(int(15 * 60 - age), reason=f"启动补齐 15m 复检 [{title_short}]")
                elif 5 * 60 <= age < 15 * 60:
                    schedule_douyin_audit(int(15 * 60 - age), reason=f"启动补齐 15m 复检 [{title_short}]")
    except Exception as e:
        log.debug("启动时恢复近期发布追踪失败：%s", e)

    consecutive_failures = 0
    while True:
        try:
            n = run_once(base, s)
            consecutive_failures = 0
            if n:
                log.info("本轮处理 %d 个平台 job", n)
            if args.once:
                break
        except WorkerError as e:
            consecutive_failures += 1
            log.error("轮询失败（连续第 %d 次）：%s", consecutive_failures, e)
            if consecutive_failures == 10:
                try:
                    telegram.notify_info(s, f"🔴 发布 worker 连续 10 轮连不上 VPS\n{str(e)[:200]}")
                except Exception:
                    pass
        except Exception:
            log.exception("worker 循环异常（继续）")
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()

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
import threading
import time
from pathlib import Path

import httpx

from .audit_check import _parse_dt
from .config import DATA_DIR, MEDIA_DIR, Settings, load_settings
from .notify import telegram

log = logging.getLogger("publish_worker")

REMOTE_DIR = MEDIA_DIR / "remote"  # 家庭端下载缓存
# 发布成功但回报失败的本地补报队列（防「已发出却回报失败 → 重复发布」）
PENDING_REPORTS_PATH = DATA_DIR / "pending_reports.jsonl"


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


def _report_success_with_retry(base: str, s: Settings, task: dict, platform: str,
                               url: str) -> bool:
    """回报「发布成功」，带退避重试 + 本地补报队列。

    视频已经真实发出去了，这条回报决定协调端把 job 记成 PUBLISHED 还是放回
    PENDING（→ 重复发布）。所以绝不允许因为隧道抖动就降级成失败：
    重试 3 次，仍失败就写本地 outbox，由下一轮启动时补报。
    """
    task_id = task["task_id"]
    for attempt in range(1, 4):
        try:
            report(base, s, task_id, platform, ok=True, url=url)
            return True
        except Exception as e:
            log.warning("[%s] %s 成功回报失败（第 %d/3 次）：%s",
                        task.get("shortcode"), platform, attempt, str(e)[:120])
            time.sleep(5 * attempt)
    _queue_pending_report(task_id, platform, url)
    log.error("[%s] %s 成功回报重试耗尽，已写入本地补报队列 %s",
              task.get("shortcode"), platform, PENDING_REPORTS_PATH)
    return False


def _queue_pending_report(task_id: int, platform: str, url: str) -> None:
    """把未能上报的成功结果落盘，等待下一轮补报（幂等追加）。"""
    try:
        PENDING_REPORTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PENDING_REPORTS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"task_id": task_id, "platform": platform,
                                 "url": url, "at": time.time()}, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error("写补报队列失败（该条可能重复发布）：%s", e)


def flush_pending_reports(base: str, s: Settings) -> int:
    """补报上轮未能上报的成功结果。返回成功条数。"""
    if not PENDING_REPORTS_PATH.exists():
        return 0
    kept: list[str] = []
    done = 0
    try:
        lines = PENDING_REPORTS_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        try:
            report(base, s, int(item["task_id"]), str(item["platform"]),
                   ok=True, url=str(item.get("url") or ""))
            done += 1
        except Exception:
            kept.append(line)
    try:
        if kept:
            PENDING_REPORTS_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")
        else:
            PENDING_REPORTS_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    if done:
        log.info("补报上一轮未上报的发布成功结果：%d 条", done)
    return done



def fetch_published(base: str, s: Settings, platform: str = "douyin",
                    days: int = 7) -> list[dict]:
    """拉 VPS 上近期已发布作品清单（审核巡检测匹配用）。"""
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{base}/api/published",
                  params={"platform": platform, "days": days}, headers=_headers(s))
        return _check(r, "拉取已发布清单").get("items", [])


def report_spark_task(base: str, s: Settings, task_id: int, platform: str,
                      spark_task: str) -> None:
    """回报本条实际挂上的星火任务名，供协调端落库做收益归因（失败静默）。"""
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{base}/api/spark_task", headers=_headers(s),
                       json={"task_id": task_id, "platform": platform,
                             "spark_task": spark_task})
            _check(r, f"回报星火任务 {task_id}")
    except Exception as e:
        log.debug("星火任务名回报失败（不影响发布）：%s", e)


def fetch_missing_urls(base: str, s: Settings, platform: str = "kuaishou",
                       days: int = 2, hours: int = 0) -> list[dict]:
    """拉协调端上已发布但链接为空的 job 清单（补链接任务用）。"""
    params = {"platform": platform, "days": days}
    if hours > 0:
        params["hours"] = hours
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{base}/api/missing-urls",
                  params=params, headers=_headers(s))
        return (_check(r, "拉取待补链接清单") or {}).get("items", [])


def report_url(base: str, s: Settings, task_id: int, platform: str, url: str) -> None:
    """补录作品公开链接到协调端（更新 publish_jobs.url 与 tasks.ks_url）。"""
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{base}/api/report-url", headers=_headers(s),
                   json={"task_id": task_id, "platform": platform, "url": url})
        _check(r, f"补录链接 {task_id}/{platform}")


def push_stats(base: str, s: Settings, platform: str, rows: list[dict]) -> dict | None:
    """把抓到的播放量回传协调端落库（失败静默，返回 None）。"""
    try:
        with httpx.Client(timeout=60) as c:
            r = c.post(f"{base}/api/stats", headers=_headers(s),
                       json={"platform": platform, "stats": rows})
            return _check(r, f"回传播放量 {platform}")
    except Exception as e:
        log.debug("播放量回传失败（不影响发布）：%s", e)
        return None


def push_hot_topics(base: str, s: Settings, platform: str, topics: list[dict]) -> dict | None:
    """把抓到的平台热门话题推回协调端储存。"""
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{base}/api/hot_topics", headers=_headers(s),
                       json={"platform": platform, "topics": topics})
            return _check(r, f"推送热门话题 {platform}")
    except Exception as e:
        log.warning("热门话题推送失败: %s", e)
        return None


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
        "first_comment": copy.get("first_comment") or task.get("first_comment") or "",
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
        spark_sink: list = []          # 快手：接收本条实际挂上的星火任务名
        plat_cfg = s.platforms.get(platform)
        if platform.startswith("kuaishou"):
            if plat_cfg:
                extra["cover_title"] = plat_cfg.cover_title
                # 热点/地点/作者声明（2026-11-04 接通：此前 config 写了但代码不读）
                if plat_cfg.anchors:
                    extra["anchors"] = dict(plat_cfg.anchors)
            fc = copy.get("first_comment")
            if not fc and s.ai_api_key:
                # 现场容错兜底：防止历史/未生成首评的任务漏发置顶首评
                try:
                    from .publish.kuaishou_reply import generate_comment_reply
                    fc = generate_comment_reply(copy.get("title") or "", author_name="")
                    log.info("[%s] 任务缺少首评，现场兜底生成: %s", task.get("shortcode"), fc)
                except Exception as e:
                    log.debug("现场生成首评兜底失败：%s", e)
            if fc:
                extra["first_comment"] = fc
            if plat_cfg and plat_cfg.attach_service:
                # 2026-11-02：星火已下架，改挂「作者服务」推广（推广任务/商品）
                extra["attach_service"] = plat_cfg.attach_service
                extra["attach_prefer"] = plat_cfg.attach_prefer
                extra["attach_fallback"] = plat_cfg.attach_fallback
                extra["spark_sink"] = spark_sink
            elif plat_cfg and plat_cfg.spark_task:
                extra["spark_task"] = True
                extra["spark_task_title"] = plat_cfg.spark_task_title
                extra["spark_sink"] = spark_sink
        elif platform == "douyin":
            # 挂载标签（商品/位置/小程序/团购/热点），配置驱动，失败不阻塞发布
            if plat_cfg and getattr(plat_cfg, "anchors", None):
                extra["anchors"] = dict(plat_cfg.anchors)
            if plat_cfg and getattr(plat_cfg, "hot_topic", ""):
                extra["hot_topic"] = plat_cfg.hot_topic
        elif platform == "toutiao":
            # 作品声明。素材是 AI 动画，配置里一般是「AI生成」。勾选失败不挡发布。
            decl = (plat_cfg.anchors.get("声明") if plat_cfg and plat_cfg.anchors else "") or ""
            if decl:
                extra["declaration"] = decl
        log.info("[%s] %s 开始发布：%s", task["shortcode"], pub.display_name, copy["title"][:40])
        try:
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
        except Exception:
            # 发布本身失败：交给外层统一回报失败（含登录态独立分支）
            raise
        # ---- 以下只在「发布已经成功」后执行，任何异常都不能走到 ok=False ----
        # 曾经的写法把 report(ok=True) 和 publish() 放在同一个 try 里：隧道抖动时
        # report 抛 WorkerError → 被 except 接住 → 回报失败 → job 回到 PENDING
        # → 下一条轮次把同一视频再发一次（重复发布）。
        _report_success_with_retry(base, s, task, platform, url or "")
        log.info("[%s] %s 发布成功：%s", task["shortcode"], pub.display_name, url or "-")
        # 星火任务名回报给协调端落库（收益归因：收益按任务维度结算）
        if spark_sink:
            try:
                report_spark_task(base, s, task_id, platform, spark_sink[0])
            except Exception as e:
                log.warning("[%s] 星火任务名回报失败（不影响发布）：%s", task["shortcode"], str(e)[:120])
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
STATS_INTERVAL = 6 * 3600  # 播放量回流：6 小时一次足够（播放数据变化慢）
_last_stats_at: float = 0.0
INCOME_INTERVAL = 6 * 3600  # 星火收益快照：6 小时一次（收益结算慢，无需频繁）
_last_income_at: float = 0.0
_scheduled_audits: list[float] = []


def maybe_refresh_stats(s: Settings, base: str = "") -> None:
    """周期性抓播放量，回传协调端落库（供话题榜使用，失败静默）。

    架构约束（实测踩过）：抖音/快手登录态只在发布端（阿里云）新鲜，
    published_at 只在协调端（德国）库里，两台各持一半。所以抓取在
    本机、匹配落库在协调端，经 POST /api/stats 桥接。
    """
    global _last_stats_at
    now = time.time()
    if now - _last_stats_at < STATS_INTERVAL:
        return
    _last_stats_at = now

    if not base:
        base = s.remote_api_url
    if not base:
        return

    from dataclasses import asdict

    from playwright.sync_api import sync_playwright

    from .monitor import stats as stats_mod
    from .publish import all_publishers, get_publisher
    from .publish.base import goto_with_retry, launch_chromium, new_context

    targets = []
    # 快手矩阵号（kuaishou / kuaishou2 / …）各有登录态，逐号抓
    ks_names = [n for n in ("kuaishou", "kuaishou2")
                if n in all_publishers()]
    for name in ("douyin", *ks_names):
        try:
            pub = get_publisher(name)
        except KeyError:
            continue
        if pub.state_path.exists():
            targets.append((name, pub))

    refreshed = 0
    with sync_playwright() as p:
        for name, pub in targets:
            try:
                browser = launch_chromium(p, headless=True)
                ctx = new_context(browser, pub.state_path)
                page = ctx.new_page()
                try:
                    url = (stats_mod.DOUYIN_MANAGE_URL if name == "douyin"
                           else stats_mod.KUAISHOU_MANAGE_URL)
                    goto_with_retry(page, url, timeout=60000)
                    got = (stats_mod.fetch_douyin_stats(page) if name == "douyin"
                           else stats_mod.fetch_kuaishou_stats(page))
                finally:
                    browser.close()
                if not got:
                    continue
                rows = [{k: v for k, v in asdict(st).items() if v is not None}
                        for st in got]
                r = push_stats(base, s, name, rows)
                refreshed += int((r or {}).get("updated") or 0)
            except Exception as e:
                log.debug("播放量抓取失败（%s，忽略）：%s", name, str(e)[:100])
    if refreshed:
        log.info("播放量回流完成：更新 %d 条（话题榜在下次生成时自动重算）", refreshed)


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


def maybe_process_login_requests(base: str, s, peek: bool = False) -> None:
    """取走协调端入队的扫码登录指令，起子进程执行 --login-qr。

    TG 发「/login weixin」→ 协调端入队 → 这里取走 → 跑登录脚本。
    二维码由登录脚本自己推 TG。子进程不阻塞主轮询。
    peek=True 只看不取（诊断用），避免误吃指令。
    """
    try:
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{base}/api/login_request", headers=_headers(s),
                      params={"peek": 1} if peek else None)
            data = r.json()
    except Exception as e:
        log.debug("取登录指令失败：%s", e)
        return
    requests_ = (data or {}).get("requests") or {}
    for platform, enqueued_at in requests_.items():
        log.info("收到 %s 扫码登录指令（%s 入队），启动登录子进程",
                 platform, enqueued_at)
        if peek:
            continue
        try:
            telegram.notify_info(s, f"🔑 开始执行 {platform} 扫码登录，二维码稍后推送到 TG")
        except Exception:
            pass
        import subprocess
        subprocess.Popen(
            [".venv/bin/python", "-m", "bot.main", "--login-qr", platform],
            cwd="/opt/ks-bot",
            stdout=open("/tmp/login_qr_out.log", "ab"),
            stderr=subprocess.STDOUT,
        )


def run_once(base: str, s: Settings) -> int:
    """一轮：拉清单 → 逐个可发布平台处理 → 审核巡检。返回处理数。"""
    from .publish import get_publisher

    # 卡死 job 回收要每轮做（幂等、开销小）：只在启动时做会漏掉「部署重启时
    # 正在发布、updated_at 还很新」的那条，它会永久停在 PUBLISHING。
    #
    # 阈值必须 > 单条正常发布的合法最长耗时，否则会把「还在跑」的 job 抢回队列，
    # 造成同一条视频重复发布。快手上传超时单独就 15 分钟（UPLOAD_TIMEOUT），
    # 加上转码等待/提交/首评，合法上限约 18~20 分钟 → 用 30 分钟留足余量。
    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(f"{base}/api/release_stale", headers=_headers(s),
                       json={"older_than_minutes": 30})
        released = (r.json() or {}).get("released", 0)
        if released:
            log.warning("回收 %d 条卡死的发布中 job（已放回待发布队列）", released)
    except Exception as e:
        log.debug("回收卡死 job 失败（不影响本轮）：%s", str(e)[:100])

    maybe_process_login_requests(base, s)
    tasks = fetch_pending(base, s)
    n = 0
    skipped_login: set[str] = set()
    for task in tasks:
        for plat in task.get("platforms", []):
            if plat.get("gate"):
                log.debug("[%s] %s gate 未放行：%s",
                          task.get("shortcode"), plat["platform"], plat["gate"])
                continue
            if plat.get("retries", 0) >= 5:
                log.warning("[%s] %s 重试已达 5 次，等人工处理", task.get("shortcode"), plat["platform"])
                continue
            # 没登录态就别认领。认领后再回报「登录失效」会把 job 打回 PENDING
            # 并每轮刷一条 TG，新平台还没扫码时会刷屏。
            try:
                pub = get_publisher(plat["platform"])
            except KeyError:
                log.warning("未知平台 %s，跳过", plat["platform"])
                continue
            if not pub.state_path.exists():
                if plat["platform"] not in skipped_login:
                    skipped_login.add(plat["platform"])
                    log.info("%s 未登录，本轮不认领（--login-qr %s）",
                             pub.display_name, plat["platform"])
                continue
            process_platform(base, s, task, plat)
            n += 1
            time.sleep(5)  # 平台间小间隔
    maybe_audit(base, s)
    maybe_refresh_stats(s, base)
    maybe_refresh_spark_income(s)
    maybe_refresh_hot_topics(s, base)
    maybe_backfill_urls(base, s)
    return n


_last_backfill_at: float = 0.0
_backfilled_task_keys: set[tuple[int, str]] = set()
BACKFILL_INTERVAL = 10 * 60      # 补链接任务最小间隔（秒）
BACKFILL_MAX_PER_RUN = 3         # 单轮最多补 3 条，避免占用发布窗口


def maybe_backfill_urls(base: str, s: Settings) -> None:
    """周期性补齐「刚发布但链接为空」的快手作品链接（每 10 分钟一次）。

    背景（2026-11-03 实测）：新作品从「审核中」进入作品管理页列表需 30~90 秒，
    发布流程内的轮询窗口偶尔赶不上，导致 TG 消息里「链接未获取到」。
    本任务在发布完成后异步兜底：查到空链接 → 按标题去管理页匹配 → 补录并 TG 提醒。

    范围限定最近 3 小时：只保单条新发布作品的链接，不回补历史存量。
    """
    global _last_backfill_at
    now = time.time()
    if now - _last_backfill_at < BACKFILL_INTERVAL:
        return
    _last_backfill_at = now

    try:
        # 只扫最近 4 小时内发布的作品：同时覆盖快手 A 号与 B 号（kuaishou / kuaishou2），
        # 仅兜底「刚发出去、现场没来得及抓到链接」的新片，不动历史存量。
        items = []
        for plat_name in ("kuaishou", "kuaishou2"):
            items.extend(fetch_missing_urls(base, s, plat_name, hours=4))
    except Exception as e:
        log.debug("补链接：拉取待补清单失败：%s", str(e)[:120])
        return
    if not items:
        return

    from .publish import get_publisher
    from .publish.kuaishou import fetch_published_url

    for item in items[:BACKFILL_MAX_PER_RUN]:
        task_id = int(item.get("task_id") or 0)
        platform = str(item.get("platform") or "kuaishou")
        title = str(item.get("title") or "")
        key = (task_id, platform)
        if not task_id or not title or key in _backfilled_task_keys:
            continue
        _backfilled_task_keys.add(key)

        try:
            pub = get_publisher(platform)
        except KeyError:
            continue
        if not pub.state_path.exists():
            continue

        try:
            url = fetch_published_url(pub.state_path, title, max_attempts=4)
        except Exception as e:
            log.debug("[%s] 补链接抓取异常：%s", item.get("shortcode"), str(e)[:120])
            url = None

        if url:
            try:
                report_url(base, s, task_id, platform, url)
                log.info("[%s] %s 链接已补录：%s",
                         item.get("shortcode"), platform, url)
            except Exception as e:
                log.warning("[%s] 补链接回报失败：%s", item.get("shortcode"), str(e)[:120])
        else:
            log.info("[%s] 补链接暂未在管理页匹配到标题，稍后再试", item.get("shortcode"))


_last_hot_topics_day: str = ""
_last_hot_topics_fail_at: float = 0.0
HOT_TOPICS_RETRY_SEC = 3600  # 失败后退避 1 小时，避免每 2 分钟锤一次快手


def maybe_refresh_hot_topics(s: Settings, base: str = "") -> None:
    """每天早上 8:00（或启动冷启动时）从快手抓取热门话题并推送到协调端。"""
    global _last_hot_topics_day, _last_hot_topics_fail_at
    if not base:
        base = s.remote_api_url.rstrip("/") if s.remote_api_url else ""
    if not base:
        return

    import time as _time
    from datetime import datetime

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # 当天已经成功拉取过则跳过
    if _last_hot_topics_day == today_str:
        return

    # 失败退避：抓取/推送失败后 1 小时内不再重试（成功才重置）
    if _time.time() - _last_hot_topics_fail_at < HOT_TOPICS_RETRY_SEC:
        return

    # 冷启动先拉一次备用，或者当前小时已达 8:00
    is_cold_start = _last_hot_topics_day == ""
    if not (is_cold_start or now.hour >= 8):
        return

    from .monitor.kuaishou_hot import fetch_kuaishou_hot_topics

    try:
        topics = fetch_kuaishou_hot_topics()
        if topics:
            res = push_hot_topics(base, s, "kuaishou", topics)
            if res and res.get("ok"):
                _last_hot_topics_day = today_str
                _last_hot_topics_fail_at = 0.0
                log.info("快手热门话题已抓取并同步至协调端: %d 条", len(topics))
            else:
                _last_hot_topics_fail_at = _time.time()
                log.warning("快手热门话题推送失败: %s（1 小时内不再重试）", res)
        else:
            _last_hot_topics_fail_at = _time.time()
            log.warning("快手热门话题未抓取到任何条目（1 小时内不再重试）")
    except Exception as e:
        _last_hot_topics_fail_at = _time.time()
        log.warning("抓取/推送快手热门话题异常: %s（1 小时内不再重试）", e)


def maybe_refresh_spark_income(s: Settings) -> None:
    """周期性抓快手星火收益快照（每 6 小时一次，失败静默）。

    只抓「累计收益/可提现余额」两个数，用于算增量。
    逐条精确归因需要逐条账单明细，PC 端接口不提供，因此按
    「任务发布量 + 期间收益增量」做均摊估算（见 monitor.spark_income）。
    """
    global _last_income_at
    now = time.time()
    if now - _last_income_at < INCOME_INTERVAL:
        return
    _last_income_at = now
    try:
        from .monitor.spark_income import record
        from .publish import all_publishers, get_publisher
        # 快手矩阵号各自抓收益快照（登录态/结算独立，文件也分开）
        for name in [n for n in ("kuaishou", "kuaishou2")
                     if n in all_publishers()]:
            try:
                pub = get_publisher(name)
            except KeyError:
                continue
            if not pub.state_path.exists():
                continue
            snap_path = (None if name == "kuaishou" else
                         pub.state_path.parent / f"{pub.state_path.stem}_income_snapshots.json")
            snap = record(headless=True, state_path=pub.state_path,
                          snapshot_path=snap_path)
            if snap:
                log.info("[%s] 星火收益快照：累计 %.2f 元", name, snap["income"])
    except Exception as e:
        log.debug("星火收益抓取失败（忽略）：%s", str(e)[:120])


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

    # 独立线程盯登录指令：60s 一次，不等发布轮次（发布中一轮可占 10+ 分钟，
    # 期间 TG 指令会延迟推码）。run_once 里保留的检查作为兜底。
    def _login_watcher() -> None:
        while True:
            try:
                maybe_process_login_requests(base, s)
            except Exception:
                log.exception("登录指令监视线程异常（继续）")
            time.sleep(60)

    if not args.once:
        threading.Thread(target=_login_watcher, name="login-watcher",
                         daemon=True).start()

    while True:
        try:
            # 每轮都做：补报上轮未上报的成功结果、释放卡死的 PUBLISHING job。
            # 曾经只在启动时清一次卡死 job：部署重启时正在发的那条 updated_at
            # 还很新，30 分钟阈值内不会被释放，之后再也没有机会 → 永久卡死。
            try:
                flush_pending_reports(base, s)
            except Exception:
                log.debug("补报队列处理失败（下轮再试）", exc_info=True)
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

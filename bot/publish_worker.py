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


def claim(base: str, s: Settings, task_id: int, platform: str) -> dict | None:
    """原子认领。返回 task payload；被别人抢先/状态不对返回 None。"""
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{base}/api/claim", headers=_headers(s),
                   json={"task_id": task_id, "platform": platform})
        data = _check(r, f"认领 {task_id}/{platform}")
    return data.get("task") if data.get("ok") else None


def report(base: str, s: Settings, task_id: int, platform: str, ok: bool,
           url: str = "", error: str = "", login_expired: bool = False) -> None:
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{base}/api/report", headers=_headers(s),
                   json={"task_id": task_id, "platform": platform, "ok": ok,
                         "url": url, "error": error, "login_expired": login_expired})
        _check(r, f"回报 {task_id}/{platform}")


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


def process_platform(base: str, s: Settings, task: dict, plat_info: dict) -> None:
    """处理单个平台 job：认领 → 下载 → 发布 → 回报。异常就地回报失败，不中断其他平台。"""
    from .publish import get_publisher
    from .publish.base import LoginExpired

    task_id, platform = task["task_id"], plat_info["platform"]
    try:
        payload = claim(base, s, task_id, platform)
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
        if platform == "kuaishou":
            plat_cfg = s.platforms.get("kuaishou")
            if plat_cfg and plat_cfg.spark_task:
                extra["spark_task"] = True
                extra["spark_task_title"] = plat_cfg.spark_task_title
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
        # 发布完成后清理该任务缓存视频
        shutil.rmtree(dest_dir, ignore_errors=True)
    except LoginExpired as e:
        log.warning("[%s] %s 登录态失效：%s", task.get("shortcode"), platform, str(e)[:120])
        report(base, s, task_id, platform, ok=False, error=str(e)[:500], login_expired=True)
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


def run_once(base: str, s: Settings) -> int:
    """一轮：拉清单 → 逐个可发布平台处理。返回处理数。"""
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
    return n


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bot.publish_worker",
                                     description="远程发布 worker（家庭端）")
    parser.add_argument("--once", action="store_true", help="跑一轮就退出（调试）")
    parser.add_argument("--interval", type=int, default=120, help="轮询间隔秒数（默认 120）")
    args = parser.parse_args()

    from .main import setup_logging
    setup_logging()
    s = load_settings()

    if not s.remote_api_url or not s.remote_api_token:
        log.error("REMOTE_API_URL / REMOTE_API_TOKEN 未配置（.env），无法连接 VPS")
        raise SystemExit(1)
    base = s.remote_api_url.rstrip("/")

    log.info("发布 worker 启动 → %s（间隔 %ds）", base, args.interval)
    try:
        telegram.notify_info(s, f"🟢 发布 worker 已启动\n远端：{base}\n间隔 {args.interval}s")
    except Exception:
        pass

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

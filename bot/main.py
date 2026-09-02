"""入口：python -m bot.main [--setup | --login | --status | --retry-failed] [--dry-run] [--once] [--headed]

主循环以 SQLite 状态机驱动每条 IG 作品：
DETECTED → DOWNLOADED → TRANSCODED → TRANSCRIBED → COPYWRITTEN
        → SUBTITLED → PUBLISHED → NOTIFIED
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import shutil
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import set_key

from .ai.asr import transcribe
from .ai.copywriter import generate_copy
from .ai.vision import check_real_person
from .config import (
    ENV_PATH,
    FINAL_DIR,
    LOGS_DIR,
    RAW_DIR,
    WORK_DIR,
    Settings,
    ensure_dirs,
    load_settings,
)
from .db import JOB_FINAL_STATES, Database, JobState, State, now_iso
from .maintenance.cleanup import purge_orphans, purge_task_media, run_cleanup
from .media import ffmpeg
from .media.subtitles import write_ass, write_srt
from .notify import telegram
from .publish import all_publishers, enabled_publishers, get_publisher
from .publish.base import LoginExpired
from .source import downloader
from .source.scheduler import DiscoveryScheduler
from .source.telegram_listener import TelegramListener

log = logging.getLogger("bot")

MAX_RETRIES = 3


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s", "%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    file_handler = RotatingFileHandler(
        LOGS_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    # httpx 默认 INFO 会把带 token 的 URL 打进日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ---------- 状态机：各阶段处理函数 ----------

def step_download(s: Settings, db: Database, task: dict) -> None:
    """用 yt-dlp 下载视频（支持 IG/YouTube/Facebook/TikTok 等）。不再依赖 IG 小号登录。"""
    url = task.get("source_url") or task.get("media_url")
    if not url:
        raise ValueError("该任务没有下载链接（source_url/media_url 均为空）")
    sc = task["shortcode"]
    raw = RAW_DIR / f"{sc}.mp4"
    if raw.exists() and raw.stat().st_size > 1000:
        # 文件已存在（可能之前下载过但状态没推进），跳过下载
        log.info("[%s] 原始视频已存在（%d KB），跳过下载", sc, raw.stat().st_size // 1024)
    else:
        downloader.download(url, raw)
    db.update(task["id"], state=State.DOWNLOADED, raw_path=str(raw), error=None)
    log.info("[%s] 原始视频下载完成（来源 @%s）", sc, task.get("username"))


def step_transcode(s: Settings, db: Database, task: dict) -> None:
    sc = task["shortcode"]
    raw = Path(task["raw_path"])
    tc = WORK_DIR / f"{sc}_tc.mp4"
    cover = WORK_DIR / f"{sc}_cover.jpg"
    ffmpeg.ensure_compatible(raw, tc)
    # 去除元数据里的作者/来源痕迹（IG 视频会带 title/comment 标签）
    ffmpeg.strip_metadata(tc, tc)
    try:
        ffmpeg.extract_cover(tc, cover)
    except ffmpeg.FFmpegError as e:
        log.warning("[%s] 封面抽取失败（不影响流程）：%s", sc, e)
    db.update(task["id"], state=State.TRANSCODED, work_path=str(tc),
              cover_path=str(cover) if cover.exists() else None, error=None)

    # 真人镜头检测：如果是真人镜头，跳过不发布
    # 例外：只发视频号的任务不受真人限制（视频号由用户手动指定，不做真人过滤）
    targets = task.get("target_platforms")
    weixin_only = targets and all(t.strip() == "weixin" for t in targets.split(",") if t.strip())
    if cover.exists() and not weixin_only:
        is_real = check_real_person(tc, cover, s)
        if is_real:
            db.update(task["id"], state=State.SKIPPED, error="真人镜头，跳过发布")
            log.info("[%s] 检测到真人镜头，跳过", sc)
            telegram.notify_info(s, f"🚫 跳过真人镜头视频\n"
                                    f"来源：@{task.get('username','')} {task.get('permalink','')}\n"
                                    f"shortcode：{sc}\n原因：AI 检测到视频含真人镜头，已自动跳过")
            return


def step_transcribe(s: Settings, db: Database, task: dict) -> None:
    sc = task["shortcode"]
    tc = Path(task["work_path"])
    segs = transcribe(tc, s)
    transcript = "".join(x.text for x in segs).strip()
    srt_path = ""
    if segs:
        write_srt(WORK_DIR / f"{sc}.srt", segs)
        srt_path = str(WORK_DIR / f"{sc}.srt")
        info = ffmpeg.video_info(tc)
        write_ass(WORK_DIR / f"{sc}.ass", segs, info["width"], info["height"], s.subtitle)
        log.info("[%s] 生成字幕 %d 条", sc, len(segs))
    else:
        log.info("[%s] 视频无人声，跳过字幕", sc)
    db.update(task["id"], state=State.TRANSCRIBED, transcript=transcript,
              srt_path=srt_path, error=None)


def step_copywrite(s: Settings, db: Database, task: dict) -> None:
    """为每个启用平台生成专属文案。去痕迹：不传原 caption，只用 ASR 转录文本。

    copy_json 存 {platform: {title, description, tags, category}}；
    同时把快手套写进 legacy 列（title/description/tags/category），兼容 --status 和旧数据。
    """
    sc = task["shortcode"]
    transcript = task.get("transcript") or ""
    copy_all: dict[str, dict] = {}
    for pub in _task_platforms(s, task):
        cats = s.platforms.get(pub.name).categories if s.platforms.get(pub.name) else []
        copy_all[pub.name] = generate_copy(transcript, "", cats, s, platform=pub.name)
    if not copy_all:  # 没有启用平台时兜底生成快手套，流水线才能继续走
        copy_all["kuaishou"] = generate_copy(transcript, "", s.categories, s, platform="kuaishou")
    (WORK_DIR / f"{sc}_copy.json").write_text(
        json.dumps(copy_all, ensure_ascii=False, indent=2), encoding="utf-8")
    ks = copy_all.get("kuaishou") or next(iter(copy_all.values()))
    db.update(task["id"], state=State.COPYWRITTEN, title=ks["title"],
              description=ks["description"], tags=" ".join(ks["tags"]),
              category=ks["category"],
              copy_json=json.dumps(copy_all, ensure_ascii=False), error=None)
    log.info("[%s] 文案生成完成（%d 个平台，全新生成无原痕迹）：%s",
             sc, len(copy_all), ks["title"])


def step_subtitle(s: Settings, db: Database, task: dict) -> None:
    sc = task["shortcode"]
    tc = Path(task["work_path"])
    final = FINAL_DIR / f"{sc}_final.mp4"
    ass = WORK_DIR / f"{sc}.ass"
    if s.subtitle.enabled and ass.exists():
        ffmpeg.burn_subtitles(tc, ass, final)
    else:
        shutil.copyfile(tc, final)
    db.update(task["id"], state=State.SUBTITLED, final_path=str(final), error=None)
    log.info("[%s] 成品就绪：%s", sc, final)


def _platform_copy(s: Settings, db: Database, task: dict, pub) -> dict:
    """取该平台文案；copy_json 里没有（平台后启用/旧任务）就现场生成并补写。"""
    fresh = db.get(task["id"]) or task  # 重新读，避免循环里多个平台互相覆盖 copy_json
    data: dict = {}
    if fresh.get("copy_json"):
        try:
            data = json.loads(fresh["copy_json"])
        except Exception:
            data = {}
    copy = data.get(pub.name)
    if copy and copy.get("title"):
        return copy
    cats = s.platforms.get(pub.name).categories if s.platforms.get(pub.name) else []
    copy = generate_copy(task.get("transcript") or "", "", cats, s, platform=pub.name)
    data[pub.name] = copy
    db.update(task["id"], copy_json=json.dumps(data, ensure_ascii=False))
    return copy


def _job_error(s: Settings, db: Database, task: dict, job: dict, pub, e: Exception,
               login_issue: bool = False) -> None:
    """单个平台 job 出错：重试计数 + TG 通知；耗尽后 FAILED（登录问题 SKIPPED），不阻塞其他平台。"""
    retries = (job["retries"] or 0) + 1
    if retries >= MAX_RETRIES:
        state = JobState.SKIPPED if login_issue else JobState.FAILED
        db.update_job(job["id"], state=state, retries=retries, error=str(e)[:500])
        telegram.notify_info(s, f"❌ [{task['shortcode']}] {pub.display_name} 连续失败 {retries} 次，"
                                f"已标记 {state}（不影响其他平台）\n错误：{str(e)[:200]}")
    else:
        db.update_job(job["id"], retries=retries, error=str(e)[:500])
        kind = "登录态失效" if login_issue else "发布出错"
        telegram.notify_info(s, f"⚠️ [{task['shortcode']}] {pub.display_name} {kind}"
                                f"（第 {retries}/{MAX_RETRIES} 次），将自动重试\n错误：{str(e)[:200]}")


def step_publish(s: Settings, db: Database, task: dict, headed: bool) -> None:
    """推进该任务所有平台的发布 job；全部到终态后 task 标 PUBLISHED。"""
    task_id = task["id"]
    target_pubs = _task_platforms(s, task)
    db.create_publish_jobs(task_id, [p.name for p in target_pubs])
    for job in db.pending_jobs(task_id):
        pub = get_publisher(job["platform"])
        # 未登录的平台直接跳过（不烧重试次数），提示一次
        if not pub.state_path.exists():
            db.update_job(job["id"], state=JobState.SKIPPED,
                          error="未登录（登录态文件不存在）")
            log.warning("[%s] %s 未登录，跳过该平台（--login %s 后自动恢复）",
                        task["shortcode"], pub.display_name, pub.name)
            continue
        reason = publish_gate(s, db, job["platform"])
        if reason:
            log.debug("[%s] %s 发布等待：%s", task["shortcode"], pub.display_name, reason)
            continue
        copy = _platform_copy(s, db, task, pub)
        try:
            extra = {}
            if pub.name == "kuaishou":
                plat = s.platforms.get("kuaishou")
                if plat and plat.spark_task:
                    extra["spark_task"] = True
                    extra["spark_task_title"] = plat.spark_task_title
            url = pub.publish(
                video=Path(task["final_path"]),
                title=copy.get("title", ""),
                description=copy.get("description", ""),
                tags=copy.get("tags") or [],
                category=copy.get("category") or None,
                cover=Path(task["cover_path"]) if task.get("cover_path") else None,
                headless=s.publish.headless and not headed,
                state_path=pub.state_path,
                **extra,
            )
            db.update_job(job["id"], state=JobState.PUBLISHED, url=url,
                          published_at=now_iso(), error=None, retries=0)
            log.info("[%s] %s 发布成功：%s", task["shortcode"], pub.display_name,
                     url or "（链接未获取到）")
            # 每个平台成功后立即通知（不等全部完成），避免几小时的静默期
            all_jobs = db.jobs(task_id)
            done = sum(1 for j in all_jobs if j["state"] == JobState.PUBLISHED)
            telegram.notify_info(s, f"✅ [{task['shortcode']}] {pub.display_name}发布成功"
                                    f"（{done}/{len(all_jobs)} 平台完成）"
                                    + (f"\n链接：{url}" if url else ""))
        except LoginExpired as e:
            _job_error(s, db, task, job, pub, e, login_issue=True)
        except Exception as e:
            _job_error(s, db, task, job, pub, e)
        time.sleep(random.uniform(3, 8))  # 平台间隔，避免连续自动化操作
    jobs = db.jobs(task_id)
    if jobs and all(j["state"] in JOB_FINAL_STATES for j in jobs):
        db.update(task_id, state=State.PUBLISHED, published_at=now_iso(), error=None)


def step_notify(s: Settings, db: Database, task: dict) -> None:
    jobs = db.jobs(task["id"])
    for j in jobs:
        try:
            j["display_name"] = get_publisher(j["platform"]).display_name
        except KeyError:
            j["display_name"] = j["platform"]
    telegram.notify_published(s, task, jobs or None)
    db.update(task["id"], state=State.NOTIFIED, error=None)


# ---------- 发布闸门 ----------

def _task_platforms(s: Settings, task: dict) -> list:
    """返回该任务要发布的平台列表（PublisherSpec）。

    task.target_platforms 为 None/空 → 所有启用平台（默认行为）。
    有值 → 只发指定的平台，不受 config enabled 限制（如 @视频号 即使 enabled=false 也发）。
    指定的平台不存在则跳过并记日志；全部无效则回退为全部启用平台。
    """
    raw = task.get("target_platforms")
    if not raw:
        return enabled_publishers(s)
    wanted = {p.strip() for p in raw.split(",") if p.strip()}
    all_pubs = all_publishers()
    pubs = [all_pubs[name] for name in wanted if name in all_pubs]
    missing = wanted - set(pubs) if pubs else wanted
    if missing:
        unknown = wanted - set(all_pubs)
        if unknown:
            log.warning("[%s] 未识别的平台名 %s，已跳过", task.get("shortcode", "?"), unknown)
    if not pubs:
        log.warning("[%s] 指定的平台全部不可用，回退为全部启用平台", task.get("shortcode", "?"))
        return enabled_publishers(s)
    return pubs


def _parse_hhmm(text: str) -> int:
    h, m = text.strip().split(":")
    return int(h) * 60 + int(m)


def publish_gate(s: Settings, db: Database, platform: str | None = None) -> str | None:
    """返回 None 表示放行；否则返回需要等待的原因。

    platform 不为空时，每日上限和最小间隔按该平台独立计算（各平台互不影响）。
    """
    pub = s.publish
    now = datetime.now()
    if pub.window:
        start, end = (_parse_hhmm(x) for x in pub.window)
        cur = now.hour * 60 + now.minute
        in_window = start <= cur < end if start <= end else (cur >= start or cur < end)
        if not in_window:
            return f"不在发布窗口 {pub.window[0]}~{pub.window[1]}"
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    if db.count_published_since(today0, platform) >= pub.daily_limit:
        return f"{platform} 已达每日发布上限 {pub.daily_limit} 条"
    last = db.last_published_at(platform)
    if last:
        next_ok = datetime.fromisoformat(last) + timedelta(hours=pub.min_gap_hours)
        if now < next_ok:
            return f"{platform} 距上次发布间隔不足（{next_ok.strftime('%H:%M')} 后放行）"
    return None


def _window_only_gate(s: Settings, db: Database) -> str | None:
    """只检查发布窗口（全局），不检查平台间隔/限额——那些在 step_publish 内按平台判断。"""
    pub = s.publish
    if pub.window:
        start, end = (_parse_hhmm(x) for x in pub.window)
        cur = datetime.now().hour * 60 + datetime.now().minute
        in_window = start <= cur < end if start <= end else (cur >= start or cur < end)
        if not in_window:
            return f"不在发布窗口 {pub.window[0]}~{pub.window[1]}"
    return None


# ---------- 状态机推进 ----------

def advance_task(s: Settings, db: Database, task_id: int,
                 dry_run: bool, headed: bool, stage: str = "all") -> None:
    for _ in range(12):  # 单个任务一轮最多推进 12 步，防异常死循环
        task = db.get(task_id)
        if task is None:
            return
        st = task["state"]
        if st == State.NOTIFIED:
            return
        try:
            if st == State.CANDIDATE:
                # 发现层入库后应已被 scheduler 推到 PENDING_REVIEW；
                # 若卡在 CANDIDATE（如重启后），补发审核卡片
                db.update(task_id, state=State.PENDING_REVIEW)
                telegram.send_review_card(s, task)
                return
            if st == State.PENDING_REVIEW:
                # 等用户点 TG 审核按钮，不自动推进
                log.debug("[%s] 等待人工审核", task["shortcode"])
                return
            if st == State.DETECTED:
                step_download(s, db, task)
            elif st == State.DOWNLOADED:
                step_transcode(s, db, task)
            elif st == State.TRANSCODED:
                step_transcribe(s, db, task)
            elif st == State.TRANSCRIBED:
                step_copywrite(s, db, task)
            elif st == State.COPYWRITTEN:
                step_subtitle(s, db, task)
            elif st in (State.SUBTITLED, State.READY):
                if dry_run:
                    if st == State.SUBTITLED:
                        db.update(task_id, state=State.READY)
                        log.info("[dry-run] %s 已就绪，停在发布前：%s", task["shortcode"], task["final_path"])
                    return
                if stage == "process":
                    # 分体部署 VPS 端：推进到 READY 即止，发布由家庭端 worker 认领
                    if st == State.SUBTITLED:
                        # 预创建平台 job：远程端 /api/pending 按 job 派发，没有 job 任务就不可见
                        db.create_publish_jobs(task_id, [p.name for p in _task_platforms(s, task)])
                        db.update(task_id, state=State.READY)
                        log.info("[stage=process] %s 处理完成，等待远程发布端领取：%s",
                                 task["shortcode"], task["final_path"])
                    return
                # 发布窗口是全局的（夜间不发），平台间隔/限额在 step_publish 内按平台独立判断
                window_reason = _window_only_gate(s, db)
                if window_reason:
                    log.debug("发布等待：%s（%s）", task["shortcode"], window_reason)
                    return
                step_publish(s, db, task, headed)
                return  # 发布阶段每轮最多推进一次：失败的 job 等下一轮再试，避免连烧伤重试次数
            elif st == State.PUBLISHED:
                step_notify(s, db, task)
            else:
                return
        except Exception as e:
            retries = (task["retries"] or 0) + 1
            log.exception("[%s] 在 %s 阶段失败（第 %d 次）", task["shortcode"], st, retries)
            if retries >= MAX_RETRIES:
                db.update(task_id, state=State.FAILED, retries=retries, error=str(e)[:500])
                telegram.notify_failed(s, task, e)
                telegram.notify_info(s, f"❌ 任务 [{task['shortcode']}] @{task.get('username','')} 在 {st} 阶段连续失败 {retries} 次，已标记 FAILED\n"
                                        f"错误：{str(e)[:200]}\n"
                                        f"修复后运行 python -m bot.main --retry-failed 重跑")
            else:
                db.update(task_id, retries=retries, error=str(e)[:500])
                telegram.notify_info(s, f"⚠️ 任务 [{task['shortcode']}] @{task.get('username','')} 在 {st} 阶段出错（第 {retries}/{MAX_RETRIES} 次），将自动重试\n"
                                        f"错误：{str(e)[:200]}")
            return


# ---------- 主循环 ----------

_last_cleanup_date: str | None = None


def cycle(s: Settings, db: Database, dry_run: bool, headed: bool, stage: str = "all") -> None:
    """每轮：自愈滞留任务 + 推进流水线 + 每日清理。

    新作品发现已改为 TG 投链驱动（TelegramListener 线程），不再自动监控 IG。
    stage="process" 时推进到 READY 即止（分体部署 VPS 端）。
    """
    global _last_cleanup_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _last_cleanup_date != today:
        _last_cleanup_date = today
        try:
            run_cleanup(db)
        except Exception:
            log.exception("清理任务失败")

    # 1) 自愈：终态任务若仍有待发布的平台 job（如人工重置 job 后忘了重开任务），
    #    重新打开为 SUBTITLED，避免状态机永远跳过它。
    try:
        reopened = db.reopen_stranded()
        if reopened:
            log.warning("自愈：重开 %d 个滞留任务（有 PENDING 平台 job）", reopened)
            telegram.notify_info(s, f"🩹 自愈：重开 {reopened} 个滞留任务（存在待发布平台 job）")
    except Exception:
        log.exception("reopen_stranded 失败")

    # 2) 推进流水线
    for task in db.pending():
        advance_task(s, db, task["id"], dry_run, headed, stage)


# ---------- 子命令 ----------

def cmd_setup(s: Settings) -> None:
    """交互式初始化向导：长效 token 交换 + 发现 IG_USER_ID + TG chat_id。"""
    print("=" * 56)
    print(" ks-bot 初始化向导")
    print("=" * 56)
    if not ENV_PATH.exists():
        shutil.copyfile(ENV_PATH.with_suffix(".example"), ENV_PATH)
        print(f"已创建 {ENV_PATH.name}，请填写后再运行 --setup")
        return

    # 1. 验证下载器（yt-dlp）
    print("\n[1/2] 验证视频下载器（yt-dlp）...")
    try:
        from .source.downloader import parse_url
        test = parse_url("https://www.instagram.com/reel/ABC123/?igsi=test&utm_source=x")
        print(f"  ✅ yt-dlp 可用，URL 规范化正常：{test}")
    except Exception as e:
        print(f"  ❌ 下载器异常：{e}")

    # 2. 验证 Telegram
    print("\n[2/2] 验证 Telegram...")
    if s.telegram_bot_token and not s.telegram_chat_id:
        ids = telegram.discover_chat_ids(s)
        if len(ids) == 1:
            set_key(str(ENV_PATH), "TELEGRAM_CHAT_ID", ids[0])
            print(f"  ✅ 已自动识别 TELEGRAM_CHAT_ID={ids[0]} 并写回 .env")
        elif ids:
            print(f"  ℹ️ 检测到多个 chat_id：{'、'.join(ids)}，请把要接收通知的那个填入 .env")
        else:
            print("  ℹ️ 未检测到 chat_id：请先给你的 bot 发一条消息，再重新运行 --setup")
    elif s.telegram_bot_token and s.telegram_chat_id:
        telegram.notify_info(s, "ks-bot --setup 验证：Telegram 通知正常 ✅")
        print("  ✅ Telegram 已配置，测试消息已发送")
    else:
        print("  ⚠️ 未配置 TELEGRAM_BOT_TOKEN")

    print("\n初始化向导完成。下一步：")
    print("  1. python -m bot.main --login all   # 各平台扫码登录（一次性，不想全登就单独指定平台名）")
    print("  2. python -m bot.main --dry-run --once  # 试跑：只处理不上传")
    print("  3. python -m bot.main           # 正式运行")


def cmd_status(db: Database) -> None:
    tasks = db.recent(30)
    if not tasks:
        print("（暂无任务。首次运行会先把存量作品记为基线。）")
        return
    print(f"{'ID':<5}{'shortcode':<12}{'状态':<12}{'分区':<8}{'标题':<24}{'发布时间':<20}")
    print("-" * 82)
    for t in tasks:
        title = (t.get("title") or "")[:22]
        print(f"{t['id']:<5}{t['shortcode'][:11]:<12}{t['state']:<12}"
              f"{(t.get('category') or '-')[:6]:<8}{title:<24}{t.get('published_at') or '':<20}")


def cmd_abandon_unpublished(s: Settings, db: Database) -> None:
    """放弃积压未发：流水线中任务 + PENDING/FAILED 平台 job 标 SKIPPED，并删对应媒体。"""
    summary = db.abandon_unpublished()
    tasks, jobs = summary["tasks"], summary["jobs"]
    if not tasks and not jobs:
        print("没有积压未发的任务或平台 job。")
        return
    print(f"放弃 {len(tasks)} 个未完成任务、{len(jobs)} 个 PENDING/FAILED 平台 job：")
    for t in tasks:
        print(f"  task [{t['shortcode']}] {t['state']} -> SKIPPED")
        purge_task_media(t, keep_final=False)
    for j in jobs:
        print(f"  job  [{j.get('shortcode', '?')}] {j['platform']} {j['state']} -> SKIPPED")
    orphans = purge_orphans(db)
    if orphans["deleted"]:
        print(f"清理孤儿媒体 {orphans['deleted']} 个（约 {orphans['freed_mb']}MB）")
    lines = [
        f"已放弃积压未发：{len(tasks)} 个任务、{len(jobs)} 个平台 job",
    ]
    for t in tasks:
        lines.append(f"• {t['shortcode']}（原 {t['state']}）")
    for j in jobs:
        lines.append(f"• {j.get('shortcode', '?')} / {j['platform']}（原 {j['state']}）")
    telegram.notify_info(s, "\n".join(lines))
    print("完成。这些视频不会再发布。")


def cmd_retry_failed(s: Settings, db: Database) -> None:
    tasks = db.failed()
    if not tasks:
        print("没有 FAILED 状态的任务。")
        return
    for task in tasks:
        if task.get("final_path") and Path(task["final_path"]).exists():
            state = State.SUBTITLED
        elif task.get("work_path") and Path(task["work_path"]).exists():
            state = State.TRANSCRIBED if task.get("title") else State.TRANSCODED
        elif task.get("raw_path") and Path(task["raw_path"]).exists():
            state = State.DOWNLOADED
        elif task.get("media_url"):
            state = State.DETECTED
        else:
            print(f"[{task['shortcode']}] 缺少下载链接和本地文件，无法重跑（等下一条新作品吧）")
            continue
        db.update(task["id"], state=state, retries=0, error=None)
        print(f"[{task['shortcode']}] FAILED -> {state}（retries 清零）")
    print(f"共重置 {len(tasks)} 个失败任务，下次循环会自动续跑。")


# ---------- 入口 ----------

def _sigterm_handler(s: Settings):
    """SIGTERM（launchd 停止/kill）时先通知再优雅退出，避免进程被无声终止。"""
    def handler(signum, frame):
        log.info("收到信号 %d，正在停止 bot", signum)
        try:
            telegram.notify_info(s, "🟠 bot 收到终止信号 (SIGTERM)，正在停止运行")
        except Exception:
            pass
        raise KeyboardInterrupt
    return handler


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m bot.main", description="投链驱动：下载处理并发布到快手/抖音/小红书/视频号")
    parser.add_argument("--setup", action="store_true", help="交互式初始化向导（IG token/ID、TG chat_id）")
    parser.add_argument("--login", nargs="?", const="kuaishou", default=None, metavar="PLATFORM",
                        help="扫码登录发布平台：kuaishou/douyin/xhs，可逗号分隔或 all（默认 kuaishou）")
    parser.add_argument("--status", action="store_true", help="查看任务列表")
    parser.add_argument("--retry-failed", action="store_true", help="重置失败任务并续跑")
    parser.add_argument("--abandon-unpublished", action="store_true",
                        help="放弃积压未发（流水线中任务 + PENDING/FAILED 平台 job），不补发")
    parser.add_argument("--dry-run", action="store_true", help="只处理不发布（停在发布前，检查 media/final/）")
    parser.add_argument("--stage", choices=("all", "process"), default="all",
                        help="all=完整流程（默认）；process=只采集处理到 READY（分体部署 VPS 端，"
                             "发布由家庭端 worker 通过 Remote API 认领）")
    parser.add_argument("--serve-api", action="store_true",
                        help="同时启动 Remote API 服务（分体部署 VPS 端；需 .env 配 REMOTE_API_TOKEN/BIND）")
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出（调试用）")
    parser.add_argument("--headed", action="store_true", help="发布时显示浏览器窗口（调试发布流程）")
    args = parser.parse_args()

    ensure_dirs()
    setup_logging()
    s = load_settings()

    if args.setup:
        cmd_setup(s)
        return

    db = Database()
    if args.login is not None:
        names = list(all_publishers()) if args.login == "all" else \
            [x.strip() for x in args.login.split(",") if x.strip()]
        ok_all = True
        for name in names:
            try:
                pub = get_publisher(name)
            except KeyError as e:
                print(f"❌ {e}")
                ok_all = False
                continue
            print(f"\n=== {pub.display_name} 扫码登录（5 分钟内有效）===")
            ok_all &= bool(pub.login(pub.state_path))
        sys.exit(0 if ok_all else 1)
    if args.status:
        cmd_status(db)
        return
    if args.retry_failed:
        cmd_retry_failed(s, db)
        return
    if args.abandon_unpublished:
        cmd_abandon_unpublished(s, db)
        return

    if not s.ai_api_key:
        log.warning("AI_API_KEY 未配置，文案生成/语音识别(api 模式)会失败")
    if not (s.telegram_bot_token and s.telegram_chat_id):
        log.warning("Telegram 未配置完整，将跳过通知和投链监听")

    log.info("bot 启动（stage=%s，dry_run=%s，轮询间隔 %d 分钟）",
             args.stage, args.dry_run, s.poll_interval_min)
    telegram.notify_info(s, f"🟢 bot 已启动（stage={args.stage}）\n"
                            f"直接发视频链接给 bot 即可（IG/YouTube/Facebook/TikTok 等）\n"
                            f"轮询间隔 {s.poll_interval_min} 分钟")
    # 被外部 kill（SIGTERM，如 launchd 停止）时也要发通知，否则进程会被无声终止
    signal.signal(signal.SIGTERM, _sigterm_handler(s))

    # 分体部署：--serve-api 或 --stage=process 时起 Remote API（家庭发布端访问）
    api_thread = None
    if args.serve_api or args.stage == "process":
        if s.remote_api_token and s.remote_api_bind:
            from .remote_api import start_api_thread
            api_thread = start_api_thread(s, db, gate_fn=lambda p: publish_gate(s, db, p))
            log.info("Remote API 已启动（%s:%d，家庭端发布 worker 接入点）",
                     s.remote_api_bind, s.remote_api_port)
        else:
            log.warning("REMOTE_API_TOKEN/REMOTE_API_BIND 未配置，Remote API 未启动")

    # 启动 TG 消息监听线程（收到链接 → 入库 → 立刻唤醒主循环）
    pipeline_wakeup = threading.Event()
    listener = TelegramListener(s, db, wakeup=pipeline_wakeup)
    if not args.once:
        listener.start()

    # 启动发现层调度线程（周期采集 → 过滤 → 入库 CANDIDATE → 发审核卡片）
    discovery = DiscoveryScheduler(s, db, wakeup=pipeline_wakeup)
    if not args.once:
        discovery.start()

    try:
        while True:
            try:
                cycle(s, db, args.dry_run, args.headed, args.stage)
            except Exception as e:
                log.exception("主循环异常")
                telegram.notify_info(s, f"🔴 主循环异常（已自动恢复）\n{str(e)[:300]}")
            if args.once:
                log.info("--once：本轮结束")
                break
            pipeline_wakeup.clear()
            pipeline_wakeup.wait(timeout=max(30, s.poll_interval_min * 60))
    except KeyboardInterrupt:
        log.info("收到退出信号，bye")
        telegram.notify_info(s, "🟡 bot 已手动停止")
    except Exception as e:
        log.exception("bot 崩溃退出")
        telegram.notify_info(s, f"🔴 bot 崩溃退出\n{str(e)[:300]}\n请检查日志 logs/bot.log 后重启")
    finally:
        listener.stop()
        discovery.stop()
        db.close()
        telegram.notify_info(s, "⚪ bot 已停止运行")


if __name__ == "__main__":
    main()

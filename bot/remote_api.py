"""远程发布 API（VPS 端）：家庭发布端通过 Tailscale 访问，取任务、拉文件、回报结果。

设计约束：
- 数据库唯一真源在 VPS，家庭端无 DB 访问，全部通过本 API。
- 只绑定 Tailscale IP（REMOTE_BIND，默认 100.x.x.x），公网不可达；Bearer token 二层防护。
- 鉴权：Authorization: Bearer <REMOTE_API_TOKEN>；异常请求 401，5 次失败封 IP 10 分钟。
- 文件下载带 sha256+size，家庭端校验完整后才会发布。

端点（全部 GET/POST JSON）：
  GET  /api/pending            待发布任务（SUBTITLED 且有 final 文件），附各平台 gate 信息
  GET  /api/file?path=...      下载成品视频/封面（路径必须位于 media/final|work 下）
  POST /api/claim              {task_id, platform} 认领一个平台 job（PENDING→PUBLISHING）
  POST /api/report             {task_id, platform, ok, url?, error?, login_expired?} 回报结果
  GET  /api/missing-urls       已发布但链接为空的 job 清单（发布端补链接用）
  POST /api/report-url         {task_id, platform, url} 补录作品公开链接
  POST /api/media-url          {task_id} 生成公网可访问的成品视频 URL（IG 发布用）
  GET  /api/published          近期已发布作品清单（审核巡检测匹配用）
  POST /api/audit              {task_id, note, deleted} 审核违规处理回报
  GET  /api/health             健康检查（token 可选，用于 worker 心跳）
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import shutil
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as _config
from .config import Settings
from .db import Database, JobState, State, now_iso

log = logging.getLogger("remote_api")

MAX_FAILED_AUTH = 5
AUTH_BAN_MINUTES = 10
PUBLISHING = "PUBLISHING"  # job 认领中状态


class RemoteApiError(Exception):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_entry(path: Path) -> dict:
    return {"path": str(path), "name": path.name,
            "size": path.stat().st_size, "sha256": _sha256(path)}


class RemoteApi:
    """API 服务容器：持 Settings/Database，提供 HTTP handler 工厂。"""

    def __init__(self, s: Settings, db: Database, gate_fn=None):
        """
        gate_fn(platform) -> str | None：发布闸门（窗口/限额/间隔），复用 main.publish_gate。
        """
        self.s = s
        self.db = db
        self.gate_fn = gate_fn
        self._auth_failures: dict[str, list[datetime]] = {}
        # TG /login 指令队列：platform -> 入队时间
        self._login_requests: dict[str, str] = {}

    # ---------- 业务逻辑 ----------

    def check_auth(self, token: str, client_ip: str) -> bool:
        now = datetime.now()
        fails = [t for t in self._auth_failures.get(client_ip, []) if now - t < timedelta(minutes=AUTH_BAN_MINUTES)]
        self._auth_failures[client_ip] = fails
        if len(fails) >= MAX_FAILED_AUTH:
            return False
        if token and token == self.s.remote_api_token:
            return True
        self._auth_failures.setdefault(client_ip, []).append(now)
        return False

    def pending_payload(self) -> dict:
        """待发布清单：SUBTITLED/READY 且 final 文件在，任务的目标平台还剩 PENDING job。

        必须在 SQL 层就把状态过滤掉（pending(states=...)）：
        否则「先 LIMIT 100 再过滤」在积压够多时会返回空清单，发布端静默停摆。
        """
        out = []
        for task in self.db.pending(limit=100, states=[State.SUBTITLED, State.READY]):
            final = task.get("final_path")
            if not final or not Path(final).exists():
                continue
            jobs = [j for j in self.db.jobs(task["id"]) if j["state"] == JobState.PENDING]
            if not jobs:
                continue
            platforms = []
            for j in jobs:
                platforms.append({
                    "platform": j["platform"],
                    "job_id": j["id"],
                    "retries": j["retries"] or 0,
                    "gate": self.gate_fn(j["platform"]) if self.gate_fn else None,
                })
            cover = task.get("cover_path")
            out.append({
                "task_id": task["id"],
                "shortcode": task["shortcode"],
                "title": task.get("title") or "",
                "description": task.get("description") or "",
                "tags": (task.get("tags") or "").split(),
                "category": task.get("category") or "",
                "source_url": task.get("source_url") or "",
                "final_file": _file_entry(Path(final)),
                "cover_file": _file_entry(Path(cover)) if cover and Path(cover).exists() else None,
                "copy_json": task.get("copy_json") or "",
                "platforms": platforms,
            })
        return {"tasks": out, "server_time": now_iso()}

    def audit_report(self, data: dict) -> dict:
        """家庭端巡检回报：记录违规通知与删除结果到对应 task 的 error 字段。"""
        task_id = int(data.get("task_id") or 0)
        if not task_id:
            return {"ok": False, "error": "task_id required"}
        note = str(data.get("note") or "")[:800]
        deleted = bool(data.get("deleted"))
        if deleted:
            self.db.update(task_id, error=f"[审核违规·已删除] {note}")
            log.warning("[audit] task %d 审核违规，作品已删除：%s", task_id, note[:120])
        else:
            self.db.update(task_id, error=f"[审核违规·删除失败] {note}")
            log.warning("[audit] task %d 审核违规，删除失败：%s", task_id, note[:120])
        return {"ok": True}

    # ---------- 星火收益归因 ----------

    def spark_task_report(self, data: dict) -> dict:
        """发布端回报：本条作品实际挂上的星火变现任务名。

        落库到 publish_jobs.spark_task，用于按任务统计产出
        （星火收益按任务维度结算，见 db.spark_task_stats）。
        """
        task_id = int(data.get("task_id") or 0)
        platform = str(data.get("platform") or "")
        spark_task = str(data.get("spark_task") or "").strip()[:200]
        if not (task_id and platform and spark_task):
            return {"ok": False, "error": "task_id/platform/spark_task required"}
        with self.db._lock:
            self.db.conn.execute(
                "UPDATE publish_jobs SET spark_task=?, updated_at=? "
                "WHERE task_id=? AND platform=?",
                (spark_task, now_iso(), task_id, platform))
            self.db.conn.commit()
        log.info("[spark] task %d 挂载任务已记录：%s", task_id, spark_task)
        return {"ok": True}

    # ---------- 播放量回流（发布端抓 → 协调端落库） ----------

    def stats_ingest(self, data: dict) -> dict:
        """发布端回传作品播放数据，persist 进 tasks 供话题榜/归因使用。

        登录态只在发布端新鲜，published_at 只在协调端库里——两台机器
        各持一半，抓取在发布端、匹配在本端，靠这个端点桥起来。
        """
        from .monitor.stats import WorkStat
        from .monitor.stats import persist as persist_stats
        platform = str(data.get("platform") or "").strip()
        rows = data.get("stats") or []
        if not (platform and isinstance(rows, list)):
            return {"ok": False, "error": "platform/stats required"}
        stats = []
        for r in rows[:200]:
            if not isinstance(r, dict):
                continue
            pc = r.get("play_count")
            if not isinstance(pc, int) or pc <= 0:
                continue
            tags = r.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            stats.append(WorkStat(
                published_at=str(r.get("published_at") or ""),
                play_count=pc,
                like_count=r.get("like_count") if isinstance(r.get("like_count"), int) else None,
                tags=[str(t)[:12] for t in tags[:10]],
                title_hint=str(r.get("title_hint") or "")[:80],
            ))
        if not stats:
            return {"ok": False, "error": "no valid rows"}
        res = persist_stats(self.db, stats, platform=platform)
        if res.get("updated"):
            log.info("[stats] %s 播放回流：%d 条落库", platform, res["updated"])
            # 播放数据回流触发：自动发掘并晋升爆款源账号入白名单
            try:
                from .source.source_accounts import promote_burst_accounts
                promoted = promote_burst_accounts(self.db, s=self.s)
                for item in promoted:
                    from .notify import telegram
                    telegram.send_text(
                        self.s,
                        f"🌟 自动捕获新爆款源账号！\n"
                        f"账号：@{item['account']}（{item['platform']}）\n"
                        f"赛道：{item['lane']}\n"
                        f"依据：实测播放量达 {item['play_count']} 次！\n"
                        f"作品：《{item['title']}》\n"
                        f"已自动晋升至白名单，后续将持续追踪该账号最新作品。"
                    )
            except Exception as pe:
                log.debug("自动发掘爆款账号跳过: %s", pe)
        return {"ok": True, **res}

    def hot_topics_ingest(self, data: dict) -> dict:
        """接收发布端（阿里云）每天推送的平台热门话题并归档到 data/kuaishou_hot_topics.json。

        校验要求（2026-11-04 加固）：
        - platform 白名单：目前只有 kuaishou 有榜单；传别的平台会覆盖掉快手榜。
        - 逐条清洗：非 dict / 空 topic 一律丢弃，topic 截断到 32 字，最多 100 条。
          下游 format_hot_topics_for_prompt 会直接 .get，脏数据会让热榜静默失效。
        - 原子写入：先写临时文件再 os.replace，避免读到半截 JSON。
        """
        from .monitor.kuaishou_hot import KS_HOT_FILE

        platform = str(data.get("platform") or "kuaishou").strip().lower()
        if platform != "kuaishou":
            return {"ok": False, "error": f"unsupported platform: {platform}"}
        topics = data.get("topics") or []
        if not isinstance(topics, list):
            return {"ok": False, "error": "topics must be a list"}

        clean: list[dict] = []
        for item in topics[:100]:
            if not isinstance(item, dict):
                continue
            topic = str(item.get("topic") or "").strip()
            if not topic:
                continue
            clean.append({
                "rank": len(clean) + 1,
                "topic": topic[:32],
                "hot_value": str(item.get("hot_value") or "").strip()[:16],
            })
        if not clean:
            return {"ok": False, "error": "no valid topics"}

        payload = {
            "platform": platform,
            "updated_at": now_iso(),
            "count": len(clean),
            "topics": clean,
        }
        tmp = KS_HOT_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(KS_HOT_FILE)
        log.info("[hot_topics] %s 热门话题已归档：%d 条 -> %s", platform, len(clean), KS_HOT_FILE)
        return {"ok": True, "count": len(clean), "updated_at": payload["updated_at"]}

    # ---------- 远程登录指令（TG /login → 协调端落标记 → 发布端取走执行） ----------

    def set_login_request(self, platform: str) -> dict:
        """记录一条登录指令。发布端 worker 轮询 /api/login_request 取走执行。"""
        platform = (platform or "").strip().lower()
        if platform not in ("weixin", "toutiao", "kuaishou", "kuaishou2", "douyin", "xhs"):
            return {"ok": False, "error": f"unsupported platform: {platform}"}
        self._login_requests[platform] = now_iso()
        log.info("[login_request] %s 扫码登录指令已入队", platform)
        return {"ok": True, "platform": platform}

    def take_login_request(self, peek: bool = False) -> dict:
        """发布端取走并清除所有待执行登录指令。peek=True 只看不取。"""
        out = dict(self._login_requests)
        if not peek:
            self._login_requests.clear()
        if out:
            log.info("[login_request] 发布端%s登录指令：%s",
                     "预览" if peek else "取走", list(out))
        return {"ok": True, "requests": out}

    def published_payload(self, platform: str = "douyin", days: int = 7) -> dict:
        """近期已发布作品清单（供家庭端审核巡检匹配通知）。"""
        from datetime import datetime as _dt
        from datetime import timedelta as _td
        cutoff = (_dt.now() - _td(days=days)).isoformat(timespec="seconds")
        rows = self.db.conn.execute(
            "SELECT j.task_id, j.platform, j.state, j.url, j.published_at, "
            "       t.shortcode, t.title, t.description, t.source_url "
            "FROM publish_jobs j JOIN tasks t ON t.id = j.task_id "
            "WHERE j.platform = ? AND j.state = 'PUBLISHED' "
            "  AND (j.published_at IS NULL OR j.published_at >= ?) "
            "ORDER BY j.published_at DESC LIMIT 200",
            (platform, cutoff)).fetchall()
        items = []
        for r in rows:
            items.append({
                "task_id": r[0],
                "platform": r[1],
                "url": r[3] or "",
                "published_at": r[4] or "",
                "shortcode": r[5] or "",
                "title": r[6] or "",
                "description": r[7] or "",
                "source_url": r[8] or "",
            })
        return {"ok": True, "platform": platform, "count": len(items), "items": items}

    def missing_url_payload(self, platform: str = "kuaishou", days: int = 2,
                            hours: int = 0) -> dict:
        """已发布但链接为空的 job 清单（发布端补链接任务用）。

        hours > 0 时按小时切窗（发布端自动兜底只补「刚发布」的作品，不动历史存量）。
        """
        from datetime import datetime as _dt
        from datetime import timedelta as _td
        delta = _td(hours=hours) if hours > 0 else _td(days=days)
        cutoff = (_dt.now() - delta).isoformat(timespec="seconds")
        rows = self.db.conn.execute(
            "SELECT j.task_id, j.platform, j.published_at, t.shortcode, t.title "
            "FROM publish_jobs j JOIN tasks t ON t.id = j.task_id "
            "WHERE j.platform = ? AND j.state = 'PUBLISHED' "
            "  AND (j.url IS NULL OR j.url = '') "
            "  AND j.published_at >= ? "
            "ORDER BY j.published_at DESC LIMIT 50",
            (platform, cutoff)).fetchall()
        items = [{
            "task_id": r[0], "platform": r[1], "published_at": r[2] or "",
            "shortcode": r[3] or "", "title": r[4] or "",
        } for r in rows]
        return {"ok": True, "platform": platform, "count": len(items), "items": items}

    def _record_risk(self, platform: str, *, ok: bool, error: str,
                     display: str = "") -> None:
        """更新平台风控状态（成功→渐进恢复 / 风控失败→递增冷却）。

        只有确认是「平台风控」的失败才冷却；登录失效与网络问题不冷却，
        否则一次网络抖动就停发半小时，属于过度反应。
        """
        from .risk import (RiskState, apply_failure, apply_success,
                           classify_failure, format_duration)

        state = self.db.get_risk_state(platform) or RiskState(platform=platform)

        if ok:
            before_level = state.risk_level
            apply_success(state)
            self.db.save_risk_state(state)
            if before_level > 0 and state.risk_level < before_level:
                try:
                    from .notify import telegram
                    telegram.notify_info(
                        self.s,
                        f"🟢 {display or platform} 风险等级恢复中："
                        f"{before_level} → {state.risk_level}")
                except Exception:
                    pass
            return

        category, signal = classify_failure(error)
        apply_failure(state, category, signal)
        self.db.save_risk_state(state)

        if category == "risk":
            remaining = state.cooldown_remaining_sec()
            log.warning("[风控] %s 命中平台风控（level=%d），冷却 %s：%s",
                        platform, state.risk_level,
                        format_duration(remaining), signal)
            try:
                from .notify import telegram
                telegram.notify_info(
                    self.s,
                    f"🛑 {display or platform} 命中平台风控\n"
                    f"原因：{signal}\n"
                    f"已暂停该平台发布 {format_duration(remaining)}"
                    f"（风险等级 {state.risk_level}/{4}）\n"
                    f"冷却期内自动跳过，无需人工干预\n"
                    f"若平台侧问题已处理，可运行：\n"
                    f"  python -m bot.main --clear-risk {platform}")
            except Exception:
                pass

    def report_url(self, data: dict) -> dict:
        """补录已发布作品的公开链接（发布端补链接任务回报）。

        仅更新 url 为空的 PUBLISHED job；同时回填 tasks.ks_url（快手）并推一条 TG。
        """
        task_id = int(data.get("task_id") or 0)
        platform = str(data.get("platform") or "").strip()
        url = str(data.get("url") or "").strip()[:500]
        if not task_id or not platform or not url:
            return {"ok": False, "error": "task_id/platform/url 必填"}

        with self.db._lock:
            cur = self.db.conn.execute(
                "UPDATE publish_jobs SET url=?, updated_at=? "
                "WHERE task_id=? AND platform=? AND state='PUBLISHED' "
                "  AND (url IS NULL OR url='')",
                (url, now_iso(), task_id, platform))
            self.db.conn.commit()
            updated = cur.rowcount
            if updated and platform.startswith("kuaishou"):
                self.db.conn.execute(
                    "UPDATE tasks SET ks_url=? WHERE id=? AND (ks_url IS NULL OR ks_url='')",
                    (url, task_id))
                self.db.conn.commit()

        task = self.db.get(task_id) or {}
        shortcode = task.get("shortcode") or str(task_id)
        if updated:
            log.info("[%s] %s 补录作品链接：%s", shortcode, platform, url)
            try:
                from .notify import telegram
                telegram.notify_info(self.s, f"🔗 [{shortcode}] 作品链接已补录\n{url}")
            except Exception:
                pass
        return {"ok": True, "updated": updated}

    def media_url_payload(self, data: dict) -> dict:
        """为 Instagram 发布生成公网可访问的媒体 URL。

        背景：Meta 的 Reels API 不接受文件上传，只接受 video_url，由 Meta 服务器
        主动 cURL 拉取。成品视频在协调端，发布端（阿里云）拿不到，因此由协调端
        生成带签名、限时有效的公网地址交给发布端透传。
        """
        from .publish.media_upload import build_public_url

        task_id = int(data.get("task_id") or 0)
        if not task_id:
            return {"ok": False, "error": "task_id 必填"}
        task = self.db.get(task_id)
        if not task:
            return {"ok": False, "error": f"任务 {task_id} 不存在"}
        final_path = task.get("final_path") or ""
        if not final_path or not Path(final_path).exists():
            return {"ok": False, "error": f"任务 {task_id} 无成品文件"}
        try:
            url = build_public_url(Path(final_path))
        except Exception as e:
            return {"ok": False, "error": str(e)[:250]}
        log.info("[%s] 已生成 IG 媒体公网地址", task.get("shortcode"))
        return {"ok": True, "url": url}

    def claim(self, task_id: int, platform: str, force: bool = False) -> dict:
        """原子认领：UPDATE ... WHERE state='PENDING' 抢占，防两端并发重复发布。
        认领前复检 gate（窗口/限额/间隔），防止 worker 批量取走同平台多条 job 后
        在冷却窗内连续发布——gate 只在 pending_payload 快照时算过一次，
        取走到实际发布之间可能已有同平台新发布落地。
        force=True 时绕过 gate 检查（用于补发或人工指定触发）。
        """
        # gate 复检（锁外执行，gate_fn 内部自加 db._lock，避免不可重入死锁）
        if self.gate_fn and not force:
            reason = self.gate_fn(platform)
            if reason:
                return {"ok": False, "error": f"发布等待：{reason}", "gate_blocked": True}
        ts = now_iso()
        with self.db._lock:
            cur = self.db.conn.execute(
                "UPDATE publish_jobs SET state=?, claimed_at=?, updated_at=? "
                "WHERE task_id=? AND platform=? AND state='PENDING'",
                (PUBLISHING, ts, ts, task_id, platform))
            self.db.conn.commit()
            claimed = cur.rowcount > 0
        if not claimed:
            return {"ok": False, "error": "job 不在 PENDING 状态（已被认领或已完成）"}
        task = self.db.get(task_id)
        if not task:
            return {"ok": False, "error": "任务不存在"}
        return {"ok": True, "task": self._task_payload(task)}

    def release_stale_jobs(self, older_than_minutes: int = 30,
                           exclude: set | None = None) -> int:
        """把卡死的 PUBLISHING job 放回 PENDING，返回释放条数。

        卡死成因：worker 在发布中途被杀（部署重启/崩溃/OOM），job 永久停在
        PUBLISHING —— 既不会被 claim（只在 PENDING 里挑），也不会被 report
        （worker 进程已死）。实测存量库里积了 3 条，最久的卡了 8 天。

        阈值默认 30 分钟：正常发布耗时 1~3 分钟，超过 30 分钟必然是异常。
        发布端每次启动时调用一次即可（幂等）。

        exclude: {(task_id, platform)}，这些 job 的视频**已经发出去了**、
        只是成功回报还排在补报队列里。绝不能把它们放回 PENDING —— 那会导致
        同一条视频被再发一次。
        """
        from datetime import datetime as _dt
        from datetime import timedelta as _td
        cutoff = (_dt.now() - _td(minutes=max(1, older_than_minutes))).isoformat(timespec="seconds")
        with self.db._lock:
            rows = self.db.conn.execute(
                "SELECT id, task_id, platform FROM publish_jobs WHERE state=? AND updated_at < ?",
                (PUBLISHING, cutoff)).fetchall()
            skip_ids = [r["id"] for r in rows
                        if exclude and (r["task_id"], r["platform"]) in exclude]
            sql = ("UPDATE publish_jobs SET state='PENDING', claimed_at=NULL, updated_at=? "
                   "WHERE state=? AND updated_at < ?")
            params: list = [now_iso(), PUBLISHING, cutoff]
            if skip_ids:
                sql += f" AND id NOT IN ({','.join('?' * len(skip_ids))})"
                params.extend(skip_ids)
            cur = self.db.conn.execute(sql, params)
            self.db.conn.commit()
            n = cur.rowcount
        if n:
            log.warning("释放卡死的 PUBLISHING job %d 条（超过 %d 分钟未回报，已放回 PENDING）",
                        n, older_than_minutes)
        if skip_ids:
            log.info("跳过 %d 条已发出但待补报的 job（避免重复发布）", len(skip_ids))
        return n

    def _task_payload(self, task: dict) -> dict:
        return {
            "task_id": task["id"], "shortcode": task["shortcode"],
            "title": task.get("title") or "", "description": task.get("description") or "",
            "tags": (task.get("tags") or "").split(), "category": task.get("category") or "",
            "copy_json": task.get("copy_json") or "",
            "final_file": _file_entry(Path(task["final_path"])) if task.get("final_path") else None,
            "cover_file": _file_entry(Path(task["cover_path"]))
            if task.get("cover_path") and Path(task["cover_path"]).exists() else None,
            "target_platforms": task.get("target_platforms") or "",
        }

    def report(self, data: dict) -> dict:
        """发布结果回报。

        ok=True → job PUBLISHED + 通知。
        登录态失效（login_expired）→ job 退回 PENDING 等重登，**不消耗 retries**；
        若同时带 unrecoverable=True（账号被封等）才置 SKIPPED。
        其他失败 → 回 PENDING 等 worker 重试（retries+1）。
        """
        task_id = int(data.get("task_id") or 0)
        platform = str(data.get("platform") or "").strip()
        if not task_id or not platform:
            return {"ok": False, "error": "task_id/platform 必填"}
        ok = bool(data.get("ok"))
        err = str(data.get("error") or "")[:500]
        login_expired = bool(data.get("login_expired"))
        url = str(data.get("url") or "")[:500]
        unrecoverable = bool(data.get("unrecoverable"))

        # 失败重试上限：远端以前没有任何终态出口 —— retries 涨到 5 之后 worker
        # 只是打一行 warning 跳过，job 永远停在 PENDING，任务永远 READY，
        # 既不发布也不通知，只能人工翻库。这里给出终态。
        MAX_JOB_RETRIES = 5

        with self.db._lock:
            # 登录态失效不能把 job 打成 SKIPPED（终态、永不重试）——
            # 那会在每次会话过期时**永久烧掉整个待发队列**，重登后也回不来，
            # 只能人工翻库恢复（2026-09-28/29 连续两次踩到）。
            # 正确做法：退回 PENDING 等重登，且不消耗 retries（不是任务本身的错）。
            # 但「不消耗 retries」若无独立计数就会无限空转、每轮一条 TG：
            # 因此登录失效单独累计 login_fails，达到 2 次才转 SKIPPED 等人工。
            recoverable = login_expired and not unrecoverable
            if ok:
                new_state = JobState.PUBLISHED
            elif recoverable:
                row = self.db.conn.execute(
                    "SELECT retries, error FROM publish_jobs "
                    "WHERE task_id=? AND platform=? AND state=?",
                    (task_id, platform, PUBLISHING)).fetchone()
                prev_err = str((row["error"] if row else "") or "")
                # 累计 marker：每次登录失效追加一个 [login_expired]，
                # 已有 1 个说明本轮是连续第 2 次 → 转 SKIPPED 等人工扫码。
                prev_fails = prev_err.count("[login_expired]")
                if prev_fails >= 1:
                    new_state = JobState.SKIPPED
                    err = f"{prev_err} 连续登录失效，已跳过等人工扫码：{err}"[:500]
                else:
                    new_state = JobState.PENDING
                    err = f"[login_expired] {err}"[:500]
            elif login_expired:
                new_state = JobState.SKIPPED
            else:
                row = self.db.conn.execute(
                    "SELECT retries FROM publish_jobs WHERE task_id=? AND platform=? AND state=?",
                    (task_id, platform, PUBLISHING)).fetchone()
                will_exceed = (row["retries"] if row else 0) + 1 >= MAX_JOB_RETRIES
                new_state = JobState.FAILED if will_exceed else JobState.PENDING
            cur = self.db.conn.execute(
                "UPDATE publish_jobs SET state=?, url=?, error=?, "
                "retries=CASE WHEN ? THEN retries ELSE retries+1 END, "
                "published_at=?, claimed_at=NULL "
                "WHERE task_id=? AND platform=? AND state=?",
                (new_state,
                 url if ok else None, err if not ok else None,
                 1 if (ok or recoverable) else 0,
                 now_iso() if ok else None,
                 task_id, platform, PUBLISHING))
            self.db.conn.commit()
            if cur.rowcount == 0:
                return {"ok": False, "error": "没有找到该 task/platform 的 PUBLISHING job"}

        task = self.db.get(task_id)
        if not task:
            return {"ok": True}
        from .publish import get_publisher
        try:
            display = get_publisher(platform).display_name
        except KeyError:
            display = platform

        from .notify import telegram
        # ---- 平台风控状态记账（bot/risk.py）----
        # 只在「发布结果」这一层记账：成功 → 渐进恢复；失败 → 分类后决定是否冷却。
        # 登录失效不算风控（该走重登），网络问题也不算（该退避重试）。
        try:
            self._record_risk(platform, ok=ok, error=err, display=display)
        except Exception as e:
            log.debug("风控状态记账失败（忽略）：%s", str(e)[:120])

        if ok:
            log.info("[%s] %s 发布成功：%s", task["shortcode"], display, url or "-")
            telegram.notify_info(self.s, f"✅ [{task['shortcode']}] {display} 发布成功（远程发布端回报）"
                                         + (f"\n链接：{url}" if url else ""))
        elif login_expired:
            # 以最终状态为准（连续失效会转 SKIPPED），不能只看 login_expired 就报「已保留」，
            # 否则 operator 被告知「重登后自动继续」，实际 job 已是终态、永远不会再发。
            if new_state == JobState.SKIPPED:
                log.warning("[%s] %s 登录态连续失效，job 已 SKIPPED", task["shortcode"], display)
                telegram.notify_info(
                    self.s, f"🔑 [{task['shortcode']}] {display} 登录态持续失效，job 已跳过\n"
                            f"请到发布端重新扫码后重置该 job：\n"
                            f"python -m bot.main --login-qr {platform}")
            else:
                log.warning("[%s] %s 登录态失效，job 退回 PENDING 等待重登",
                            task["shortcode"], display)
                telegram.notify_info(
                    self.s,
                    f"🔑 [{task['shortcode']}] {display} 登录态失效\n"
                    f"job 已保留（未丢弃），重登后自动继续\n"
                    f"请到发布端运行 python -m bot.main --login-qr {platform} 重新扫码")
        else:
            if new_state == JobState.FAILED:
                log.error("[%s] %s 重试已达上限，job 标记 FAILED：%s",
                          task["shortcode"], display, err[:150])
                telegram.notify_info(
                    self.s, f"❌ [{task['shortcode']}] {display} 连续失败 {MAX_JOB_RETRIES} 次，"
                            f"job 已标记 FAILED，等人工处理后重置\n错误：{err[:250]}")
            else:
                log.warning("[%s] %s 发布失败：%s", task["shortcode"], display, err[:150])
                telegram.notify_info(self.s, f"⚠️ [{task['shortcode']}] {display} 发布失败，将自动重试\n错误：{err[:200]}")

        # 全部 job 终态 → 任务 PUBLISHED，但必须至少有一个真的发成功过。
        # 2026-11-04：新增 FAILED 终态后，若所有平台都失败 5 次，remaining 也会是 0。
        # 那时把任务标 PUBLISHED 会让 step_notify 推送「✅ 发布完成」而实际一条都没发出去，
        # 与 finalize_orphan_ready 里的不变量（必须至少一个真 PUBLISHED）矛盾。
        with self.db._lock:
            cur2 = self.db.conn.execute(
                "SELECT COUNT(*) c FROM publish_jobs WHERE task_id=? AND state NOT IN ('PUBLISHED','SKIPPED','FAILED')",
                (task_id,))
            remaining = cur2.fetchone()[0]
            done = self.db.conn.execute(
                "SELECT COUNT(*) c FROM publish_jobs WHERE task_id=? AND state='PUBLISHED'",
                (task_id,)).fetchone()[0]
        if remaining == 0 and done > 0:
            self.db.update(task_id, state=State.PUBLISHED, published_at=now_iso(), error=None)
        elif remaining == 0:
            # 全平台失败/跳过：留给人工判断，不要伪造「发布完成」
            log.error("[%s] 所有平台均未发布成功（FAILED/SKIPPED），任务保持 %s 等人工处理",
                      task["shortcode"], task["state"])
            telegram.notify_info(
                self.s, f"❌ [{task['shortcode']}] 所有平台均未发布成功，"
                        f"任务未标记完成，请人工处理后重置")
        return {"ok": True}

    # ---------- HTTP 服务 ----------

    def serve_forever(self, bind_host: str, port: int) -> None:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # 静默默认访问日志，走自己的结构化日志
                pass

            def _authed(self) -> bool:
                auth = self.headers.get("Authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else ""
                ip = self.client_address[0]
                return api.check_auth(token, ip)

            def _send(self, code: int, payload: dict | bytes, content_type: str = "application/json; charset=utf-8"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def _send_json(self, code: int, payload: dict):
                self._send(code, payload)

            def do_GET(self):
                try:
                    self._do_get()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    log.exception("GET 处理异常 %s", self.path[:120])
                    try:
                        self._send_json(400, {"error": f"bad request: {str(e)[:150]}"})
                    except Exception:
                        pass

            @staticmethod
            def _qint(qs: dict, key: str, default: int) -> int:
                """畸形查询参数不该让 handler 抛 ValueError 断连（worker 侧看到连接重置）。"""
                try:
                    return int(qs.get(key, [str(default)])[0] or default)
                except (TypeError, ValueError):
                    return default

            def _do_get(self):
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                if parsed.path == "/api/health":
                    # 健康检查不强制鉴权（worker 用来探活），但仍要 token 正确才返回详细内容
                    if not api.s.remote_api_token or self._authed():
                        self._send_json(200, {"ok": True, "time": now_iso()})
                    else:
                        self._send_json(200, {"ok": True})
                    return
                if not self._authed():
                    self._send_json(401, {"error": "unauthorized"})
                    return
                if parsed.path == "/api/pending":
                    self._send_json(200, api.pending_payload())
                    return
                if parsed.path == "/api/published":
                    plat = qs.get("platform", ["douyin"])[0]
                    days = self._qint(qs, "days", 7)
                    self._send_json(200, api.published_payload(plat, days))
                    return
                if parsed.path == "/api/missing-urls":
                    plat = qs.get("platform", ["kuaishou"])[0]
                    days = self._qint(qs, "days", 2)
                    hours = self._qint(qs, "hours", 0)
                    self._send_json(200, api.missing_url_payload(plat, days, hours))
                    return
                if parsed.path == "/api/login_request":
                    # 发布端轮询取走登录指令（GET = 取走并清除；?peek=1 只看不取）
                    self._send_json(200, api.take_login_request(
                        peek=bool(self._qint(qs, "peek", 0))))
                    return
                if parsed.path == "/api/file":
                    self._serve_file(qs.get("path", [""])[0])
                    return
                self._send_json(404, {"error": "not found"})

            def _serve_file(self, path: str):
                p = Path(path).resolve()
                # 路径白名单：成品/工作区目录，防止任意文件读取
                # （运行时读 config 模块属性，测试里 monkeypatch 才生效）
                allowed_roots = [_config.FINAL_DIR.resolve(), _config.WORK_DIR.resolve()]
                if not any(p.is_relative_to(root) for root in allowed_roots):
                    self._send_json(403, {"error": "path not allowed"})
                    return
                if not p.is_file():
                    self._send_json(404, {"error": "file not found"})
                    return
                # 分块写：成品视频可能上百 MB，read_bytes 会整片进内存
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(p.stat().st_size))
                self.end_headers()
                with p.open("rb") as fh:
                    shutil.copyfileobj(fh, self.wfile, length=1 << 20)

            def do_POST(self):
                parsed = urlparse(self.path)
                if not self._authed():
                    self._send_json(401, {"error": "unauthorized"})
                    return
                try:
                    # max(0, ...)：Content-Length 为负时 read(-1) 会一直读到 EOF，
                    # 把 handler 线程挂死。上限 1MB 防止超大 body 占内存。
                    length = max(0, min(int(self.headers.get("Content-Length") or 0), 1 << 20))
                    data = json.loads(self.rfile.read(length) or b"{}")
                except Exception:
                    self._send_json(400, {"error": "invalid json"})
                    return
                if parsed.path == "/api/claim":
                    try:
                        self._send_json(200, api.claim(int(data.get("task_id") or 0),
                                                       str(data.get("platform") or ""),
                                                       force=bool(data.get("force"))))
                    except Exception as e:
                        self._send_json(500, {"ok": False, "error": str(e)[:200]})
                    return
                if parsed.path == "/api/release_stale":
                    # 发布端每轮调用：把卡死的 PUBLISHING job 放回 PENDING。
                    # 卡死成因：worker 在发布中途被杀（部署/崩溃），job 永远
                    # 停在 PUBLISHING，既不会被 claim 也不会被 report。
                    # exclude 里的 ("task_id:platform") 是已发出但待补报的 job，
                    # 放回队列会导致重复发布。
                    try:
                        raw_ex = data.get("exclude") or []
                        skip: set[tuple[int, str]] = set()
                        if isinstance(raw_ex, list):
                            for it in raw_ex[:200]:
                                try:
                                    tid_s, plat_s = str(it).split(":", 1)
                                    skip.add((int(tid_s), plat_s))
                                except (ValueError, TypeError):
                                    continue
                        n = api.release_stale_jobs(
                            older_than_minutes=int(data.get("older_than_minutes") or 30),
                            exclude=skip)
                        self._send_json(200, {"ok": True, "released": n})
                    except Exception as e:
                        self._send_json(500, {"ok": False, "error": str(e)[:200]})
                    return
                if parsed.path == "/api/audit":
                    try:
                        self._send_json(200, api.audit_report(data))
                    except Exception as e:
                        self._send_json(500, {"ok": False, "error": str(e)[:200]})
                    return
                if parsed.path == "/api/spark_task":
                    # 发布端回报本条挂载的星火任务名（收益归因）
                    try:
                        self._send_json(200, api.spark_task_report(data))
                    except Exception as e:
                        self._send_json(500, {"ok": False, "error": str(e)[:200]})
                    return
                if parsed.path == "/api/login_request":
                    # TG /login → 协调端入队（POST = 设置）
                    try:
                        self._send_json(200, api.set_login_request(
                            str(data.get("platform") or "")))
                    except Exception as e:
                        self._send_json(500, {"ok": False, "error": str(e)[:200]})
                    return
                if parsed.path == "/api/stats":
                    # 发布端回传平台侧播放量（登录态在发布端，库在协调端，
                    # 中间靠这个端点桥接）→ persist 进本库供话题榜归因
                    try:
                        self._send_json(200, api.stats_ingest(data))
                    except Exception as e:
                        self._send_json(500, {"ok": False, "error": str(e)[:200]})
                    return
                if parsed.path == "/api/report-url":
                    # 发布端事后补录作品公开链接（发布时未抓到链接的兜底）
                    try:
                        self._send_json(200, api.report_url(data))
                    except Exception as e:
                        self._send_json(500, {"ok": False, "error": str(e)[:200]})
                    return
                if parsed.path == "/api/media-url":
                    # Instagram 发布用：协调端生成公网可访问的成品视频 URL
                    try:
                        self._send_json(200, api.media_url_payload(data))
                    except Exception as e:
                        self._send_json(500, {"ok": False, "error": str(e)[:200]})
                    return
                if parsed.path == "/api/hot_topics":
                    # 发布端每日推送快手等平台热门话题榜单（国内抓取→协调端存储）
                    try:
                        self._send_json(200, api.hot_topics_ingest(data))
                    except Exception as e:
                        self._send_json(500, {"ok": False, "error": str(e)[:200]})
                    return
                if parsed.path == "/api/report":
                    try:
                        self._send_json(200, api.report(data))
                    except Exception as e:
                        self._send_json(500, {"ok": False, "error": str(e)[:200]})
                    return
                self._send_json(404, {"error": "not found"})

        host = bind_host.strip()
        # 绑定安全校验：只允许回环与明确的私有/内网段。
        # 2026-11-04 修正：此前用 `ip.is_private` 白名单，但 0.0.0.0 / :: （未指定
        # 地址）在 Python 3.12 里 is_private 也是 True，最常见的误配会直接监听
        # 全网卡、公网可达，只剩 Bearer token 一层防护。改为显式网段白名单。
        ALLOWED_NETS = (
            ipaddress.ip_network("127.0.0.0/8"),      # 回环（SSH 隧道落地）
            ipaddress.ip_network("100.64.0.0/10"),    # Tailscale CGNAT
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("::1/128"),          # IPv6 回环
            ipaddress.ip_network("fd7a:115c:a1e0::/48"),  # Tailscale IPv6
        )
        try:
            ip = ipaddress.ip_address(host)
        except ValueError as e:
            raise RemoteApiError(
                f"REMOTE_API_BIND 不是合法 IP：{host!r}（应填 127.0.0.1 或 Tailscale IP）") from e
        if ip.is_unspecified or ip.is_link_local or ip.is_multicast:
            raise RemoteApiError(
                f"拒绝绑定 {host}：0.0.0.0/::/链路本地地址会暴露到公网，"
                f"请填 127.0.0.1（SSH 隧道）或 Tailscale IP（100.x）")
        # IPv4 映射的 IPv6（::ffff:8.8.8.8）要按 v4 判定，否则会绕过白名单
        check_ip = getattr(ip, "ipv4_mapped", None) or ip
        if not any(check_ip in net for net in ALLOWED_NETS):
            raise RemoteApiError(
                f"拒绝绑定非私有地址 {host}：API 只能绑 Tailscale IP（100.x/CGNAT）"
                f"或 127.0.0.1")

        server = ThreadingHTTPServer((host, port), Handler)
        self.server = server  # 暴露给测试/管理代码（读 server.server_address[1] 取实际端口）
        log.info("Remote API 监听 http://%s:%d（Tailscale 内网）",
                 host, server.server_address[1])
        server.serve_forever()


def start_api_thread(s: Settings, db: Database, gate_fn) -> threading.Thread:
    """起一个 daemon 线程跑 API 服务；bind/port 从 Settings.remote_api_bind/port 读。"""
    global _shared_api
    api = RemoteApi(s, db, gate_fn=gate_fn)
    _shared_api = api
    t = threading.Thread(
        target=api.serve_forever, args=(s.remote_api_bind, s.remote_api_port),
        name="remote-api", daemon=True)
    t.start()
    return t


def trigger_remote_login(s: Settings, platform: str) -> bool:
    """协调端 TG 监听调用：把登录指令入队，发布端 worker 轮询取走执行。

    注意这不是 HTTP 转发，而是写本进程 RemoteApi 的内存队列——
    TG 监听和 remote_api 跑在同一个协调端进程里。
    """
    del s  # 同进程，不需要网络调用
    global _shared_api
    if _shared_api is None:
        return False
    result = _shared_api.set_login_request(platform)
    return bool(result.get("ok"))


_shared_api: "RemoteApi | None" = None

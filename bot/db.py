"""SQLite 状态库：每条 IG 作品一个 task，沿状态机推进。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .config import DB_PATH


class State:
    CANDIDATE = "CANDIDATE"          # 发现层入库，待过滤/待发审核卡片
    PENDING_REVIEW = "PENDING_REVIEW"  # 已发 TG 审核卡片，等用户点按钮
    DETECTED = "DETECTED"            # 审核通过，待下载（原流水线入口）
    DOWNLOADED = "DOWNLOADED"        # 已下载原始视频
    TRANSCODED = "TRANSCODED"        # 已转码 + 封面
    TRANSCRIBED = "TRANSCRIBED"      # 已语音识别（可能无语音）
    COPYWRITTEN = "COPYWRITTEN"      # 已生成标题/说明/标签/分区
    SUBTITLED = "SUBTITLED"          # 已烧录字幕（成品就绪）
    READY = "READY"                  # dry-run 模式停在发布前
    PUBLISHED = "PUBLISHED"          # 已发布到快手
    NOTIFIED = "NOTIFIED"            # 已发送 TG 通知（终态）
    FAILED = "FAILED"                # 重试耗尽，终态（--retry-failed 可重跑）
    SKIPPED = "SKIPPED"              # 非视频作品等，跳过
    BASELINE = "BASELINE"            # 首次运行的存量作品，只记录不处理


# 流水线上待推进的状态，按顺序排列
# CANDIDATE/PENDING_REVIEW 在 advance_task 里只发卡片/不推进，但仍需被 cycle() 扫到
PENDING_STATES = [
    State.CANDIDATE, State.PENDING_REVIEW,
    State.DETECTED, State.DOWNLOADED, State.TRANSCODED, State.TRANSCRIBED,
    State.COPYWRITTEN, State.SUBTITLED, State.READY, State.PUBLISHED,
]


class JobState:
    PENDING = "PENDING"      # 待发布
    PUBLISHED = "PUBLISHED"  # 该平台已发布成功
    FAILED = "FAILED"        # 该平台重试耗尽（不阻塞其他平台）
    SKIPPED = "SKIPPED"      # 登录失效超限等，放弃该平台


JOB_FINAL_STATES = (JobState.PUBLISHED, JobState.FAILED, JobState.SKIPPED)

_COLUMNS = {
    "state", "error", "retries", "transcript", "title", "description", "tags",
    "category", "ks_url", "copy_json", "raw_path", "work_path", "final_path",
    "cover_path", "srt_path", "published_at", "updated_at", "media_url",
    "source_url", "source_platform", "target_platforms", "source_tag",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ig_media_id TEXT UNIQUE NOT NULL,
    shortcode TEXT NOT NULL,
    media_type TEXT,
    media_product_type TEXT,
    caption TEXT,
    permalink TEXT,
    username TEXT,
    ig_published_at TEXT,
    media_url TEXT,
    state TEXT NOT NULL DEFAULT 'DETECTED',
    error TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    transcript TEXT,
    title TEXT,
    description TEXT,
    tags TEXT,
    category TEXT,
    ks_url TEXT,
    copy_json TEXT,
    raw_path TEXT,
    work_path TEXT,
    final_path TEXT,
    cover_path TEXT,
    srt_path TEXT,
    published_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_url TEXT,
    source_platform TEXT,
    source_tag TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE TABLE IF NOT EXISTS publish_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    url TEXT,
    error TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, platform)
);
CREATE INDEX IF NOT EXISTS idx_jobs_task ON publish_jobs(task_id);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        import threading
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self) -> None:
        """存量库升级：加新列、回填 kuaishou job。幂等。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        if "copy_json" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN copy_json TEXT")
        if "source_url" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN source_url TEXT")
        if "source_platform" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN source_platform TEXT")
        if "target_platforms" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN target_platforms TEXT")
        if "source_tag" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN source_tag TEXT")
        # source_url 去重后建 UNIQUE 索引（NULL/空串允许多行）
        dups = self.conn.execute(
            """SELECT source_url FROM tasks
               WHERE source_url IS NOT NULL AND source_url != ''
               GROUP BY source_url HAVING COUNT(*) > 1"""
        ).fetchall()
        for row in dups:
            ids = [r[0] for r in self.conn.execute(
                "SELECT id FROM tasks WHERE source_url=? ORDER BY id", (row[0],)
            ).fetchall()]
            for extra_id in ids[1:]:
                self.conn.execute("DELETE FROM publish_jobs WHERE task_id=?", (extra_id,))
                self.conn.execute("DELETE FROM tasks WHERE id=?", (extra_id,))
        self.conn.execute("DROP INDEX IF EXISTS idx_tasks_source_url")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_source_url "
            "ON tasks(source_url) WHERE source_url IS NOT NULL AND source_url != ''"
        )
        # 单平台时代已发布的任务，回填为 kuaishou 平台 job，避免被新代码重新发布
        self.conn.execute(
            """INSERT OR IGNORE INTO publish_jobs
               (task_id, platform, state, url, published_at, created_at, updated_at)
               SELECT id, 'kuaishou', 'PUBLISHED', ks_url, published_at, published_at, published_at
               FROM tasks
               WHERE state IN (?, ?) AND published_at IS NOT NULL AND ks_url IS NOT NULL""",
            (State.PUBLISHED, State.NOTIFIED),
        )
        # 快手发了但没抓到链接的（ks_url 为空），按任务时间回填
        self.conn.execute(
            """INSERT OR IGNORE INTO publish_jobs
               (task_id, platform, state, url, published_at, created_at, updated_at)
               SELECT id, 'kuaishou', 'PUBLISHED', NULL, published_at, published_at, published_at
               FROM tasks
               WHERE state IN (?, ?) AND published_at IS NOT NULL AND ks_url IS NULL""",
            (State.PUBLISHED, State.NOTIFIED),
        )

    # ---------- publish_jobs ----------

    def create_publish_jobs(self, task_id: int, platforms: list[str]) -> None:
        """为任务创建各平台的 PENDING job（已存在则忽略）。"""
        ts = now_iso()
        with self._lock:
            self.conn.executemany(
                """INSERT OR IGNORE INTO publish_jobs
                   (task_id, platform, state, created_at, updated_at)
                   VALUES (?, ?, 'PENDING', ?, ?)""",
                [(task_id, p, ts, ts) for p in platforms],
            )
            self.conn.commit()

    def jobs(self, task_id: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM publish_jobs WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def pending_jobs(self, task_id: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM publish_jobs WHERE task_id=? AND state='PENDING' ORDER BY id",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_job(self, job_id: int, **fields) -> None:
        assert fields, "no fields"
        allowed = {"state", "url", "error", "retries", "published_at"}
        bad = set(fields) - allowed
        assert not bad, f"unknown job columns: {bad}"
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE publish_jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
            self.conn.commit()

    def count_published_since(self, since_iso: str, platform: str | None = None) -> int:
        """发布计数：优先 publish_jobs（多平台时代），无记录时回退旧 tasks 计数。"""
        with self._lock:
            if platform:
                row = self.conn.execute(
                    "SELECT COUNT(*) c FROM publish_jobs WHERE state=? AND platform=? AND published_at>=?",
                    (JobState.PUBLISHED, platform, since_iso),
                ).fetchone()
                return row["c"]
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM publish_jobs WHERE state=? AND published_at>=?",
                (JobState.PUBLISHED, since_iso),
            ).fetchone()
            if row["c"]:
                return row["c"]
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM tasks WHERE state IN (?,?) AND published_at >= ?",
                (State.PUBLISHED, State.NOTIFIED, since_iso),
            ).fetchone()
        return row["c"]

    def last_published_at(self, platform: str | None = None) -> str | None:
        """最近一次发布时间。传入 platform 则只查该平台（没发过返回 None）；否则全局最近。"""
        with self._lock:
            if platform:
                row = self.conn.execute(
                    "SELECT MAX(published_at) m FROM publish_jobs "
                    "WHERE published_at IS NOT NULL AND platform=?",
                    (platform,),
                ).fetchone()
                return row["m"]
            row = self.conn.execute(
                "SELECT MAX(published_at) m FROM publish_jobs WHERE published_at IS NOT NULL"
            ).fetchone()
            if row["m"]:
                return row["m"]
            row = self.conn.execute(
                "SELECT MAX(published_at) m FROM tasks WHERE published_at IS NOT NULL"
            ).fetchone()
        return row["m"]

    def insert_media(self, post, state: str, error: str | None = None) -> int | None:
        """插入一条 IG 作品（post 为 monitor.instagram.IgPost）。已存在时返回 None。"""
        ts = now_iso()
        from datetime import datetime as _dt
        ig_ts = _dt.fromtimestamp(post.timestamp).isoformat(timespec="seconds") if post.timestamp else ts
        with self._lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO tasks
                   (ig_media_id, shortcode, media_type, media_product_type, caption, permalink,
                    username, ig_published_at, media_url, state, error, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    post.media_id, post.shortcode, post.media_type, None,
                    post.caption, post.permalink, post.username,
                    ig_ts, post.video_url, state, error, ts, ts,
                ),
            )
            self.conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def find_by_source_url(self, url: str) -> dict | None:
        """按规范化 source_url 查找已有任务（去重用）。"""
        if not url:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE source_url = ? LIMIT 1", (url,)
            ).fetchone()
        return dict(row) if row else None

    def insert_video(self, meta, target_platforms: list[str] | None = None,
                     state: str = State.DETECTED, source_tag: str = "") -> int | None:
        """插入一条来自投链/发现的视频（meta 为 source.downloader.VideoMeta）。

        去重键：source_url UNIQUE。已存在返回 None。
        新任务的 ig_media_id 用 shortcode 占位（兼容旧 NOT NULL UNIQUE 约束）。
        target_platforms: 指定发布平台列表（如 ["douyin","xhs"]），None 表示全部启用平台。
        state: 入库状态。投链走 DETECTED（默认）；发现层走 CANDIDATE。
        source_tag: 发现层的来源标签（如 yt_search:anime），用于追溯；投链留空。
        """
        ts = now_iso()
        targets_str = ",".join(target_platforms) if target_platforms else None
        with self._lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO tasks
                   (ig_media_id, shortcode, media_type, caption, permalink, username,
                    media_url, source_url, source_platform, target_platforms, source_tag,
                    state, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    meta.shortcode,       # ig_media_id 列复用为通用唯一键
                    meta.shortcode,
                    "VIDEO",
                    meta.caption,
                    meta.permalink,
                    meta.username,
                    meta.source_url,      # media_url 存原始链接，下载时用
                    meta.source_url,
                    meta.platform,
                    targets_str,
                    source_tag,
                    state,
                    ts, ts,
                ),
            )
            self.conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def approve(self, task_id: int, target_platforms: list[str] | None = None) -> bool:
        """审核通过：CANDIDATE/PENDING_REVIEW → DETECTED，进原流水线。

        target_platforms 不为空时同时更新发布平台（用户在 TG 里指定了平台）。
        返回是否成功（任务不在审核态返回 False）。
        """
        ts = now_iso()
        targets_str = ",".join(target_platforms) if target_platforms else None
        with self._lock:
            cur = self.conn.execute(
                """UPDATE tasks SET state=?, error=NULL, retries=0, updated_at=?
                   WHERE id=? AND state IN (?, ?)""",
                (State.DETECTED, ts, task_id, State.CANDIDATE, State.PENDING_REVIEW),
            )
            if targets_str is not None and cur.rowcount:
                self.conn.execute(
                    "UPDATE tasks SET target_platforms=?, updated_at=? WHERE id=?",
                    (targets_str, ts, task_id),
                )
            self.conn.commit()
            return cur.rowcount > 0

    def reject(self, task_id: int, reason: str = "") -> bool:
        """审核拒绝：CANDIDATE/PENDING_REVIEW → SKIPPED。"""
        ts = now_iso()
        with self._lock:
            cur = self.conn.execute(
                """UPDATE tasks SET state=?, error=?, updated_at=?
                   WHERE id=? AND state IN (?, ?)""",
                (State.SKIPPED, f"审核拒绝: {reason}" if reason else "审核拒绝",
                 ts, task_id, State.CANDIDATE, State.PENDING_REVIEW),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def pending_review(self, limit: int = 50) -> list[dict]:
        """待审核任务列表（重启后重发卡片用）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE state IN (?, ?) ORDER BY id ASC LIMIT ?",
                (State.CANDIDATE, State.PENDING_REVIEW, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def pending_review_count(self) -> int:
        """待审核任务数量（堆积保护用）。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM tasks WHERE state IN (?, ?)",
                (State.CANDIDATE, State.PENDING_REVIEW),
            ).fetchone()
        return row["c"]

    def update(self, task_id: int, **fields) -> None:
        assert fields, "no fields"
        bad = set(fields) - _COLUMNS
        assert not bad, f"unknown columns: {bad}"
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))
            self.conn.commit()

    def get(self, task_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def pending(self, limit: int = 50) -> list[dict]:
        placeholders = ",".join("?" * len(PENDING_STATES))
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT * FROM tasks WHERE state IN ({placeholders})
                    ORDER BY COALESCE(ig_published_at, created_at) ASC, id ASC LIMIT ?""",
                (*PENDING_STATES, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def known_shortcodes(self) -> set[str]:
        with self._lock:
            rows = self.conn.execute("SELECT shortcode FROM tasks").fetchall()
        return {r["shortcode"] for r in rows}

    def is_empty(self) -> bool:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 0

    def reopen_stranded(self) -> int:
        """自愈：终态（PUBLISHED/NOTIFIED）任务若还有 PENDING 的平台 job，重新打开为 SUBTITLED。

        场景：某平台失败耗尽后任务整体完成，后来人工修复并把该平台 job 重置为 PENDING——
        任务本身也要重新打开，否则状态机会永远跳过它。"""
        with self._lock:
            cur = self.conn.execute(
                """UPDATE tasks SET state=?, error=NULL, updated_at=?
                   WHERE state IN (?, ?) AND EXISTS (
                       SELECT 1 FROM publish_jobs j WHERE j.task_id = tasks.id AND j.state = 'PENDING')""",
                (State.SUBTITLED, now_iso(), State.PUBLISHED, State.NOTIFIED),
            )
            self.conn.commit()
        return cur.rowcount

    def abandon_unpublished(self) -> dict:
        """放弃未完成发布：流水线中任务 + PENDING/FAILED 平台 job 标 SKIPPED。

        已 NOTIFIED 的任务本身不动（其他平台可能已发出），只放弃其失败/待发的 job。
        """
        abandonable = (
            State.CANDIDATE, State.PENDING_REVIEW,
            State.DETECTED, State.DOWNLOADED, State.TRANSCODED, State.TRANSCRIBED,
            State.COPYWRITTEN, State.SUBTITLED, State.READY,
        )
        ts = now_iso()
        ph = ",".join("?" * len(abandonable))
        with self._lock:
            tasks = [dict(r) for r in self.conn.execute(
                f"SELECT * FROM tasks WHERE state IN ({ph})", abandonable
            ).fetchall()]
            jobs = [dict(r) for r in self.conn.execute(
                """SELECT j.*, t.shortcode FROM publish_jobs j
                   JOIN tasks t ON t.id = j.task_id
                   WHERE j.state IN (?, ?)""",
                (JobState.PENDING, JobState.FAILED),
            ).fetchall()]
            for t in tasks:
                self.conn.execute(
                    "UPDATE tasks SET state=?, error=?, retries=0, updated_at=? WHERE id=?",
                    (State.SKIPPED, "abandoned", ts, t["id"]),
                )
            for j in jobs:
                self.conn.execute(
                    "UPDATE publish_jobs SET state=?, error=?, updated_at=? WHERE id=?",
                    (JobState.SKIPPED, "abandoned", ts, j["id"]),
                )
            self.conn.commit()
        return {"tasks": tasks, "jobs": jobs}

    def cleanup_candidates(self) -> list[dict]:
        """终态任务（含 SKIPPED），供媒体清理使用。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, shortcode, state, raw_path, work_path, final_path, updated_at "
                "FROM tasks WHERE state IN (?, ?, ?)",
                (State.NOTIFIED, State.FAILED, State.SKIPPED),
            ).fetchall()
        return [dict(r) for r in rows]

    def keep_media_shortcodes(self) -> set[str]:
        """仍在流水线或已发布的 shortcode，孤儿文件清理时保留。"""
        keep = (
            State.CANDIDATE, State.PENDING_REVIEW,
            State.DETECTED, State.DOWNLOADED, State.TRANSCODED, State.TRANSCRIBED,
            State.COPYWRITTEN, State.SUBTITLED, State.READY, State.PUBLISHED, State.NOTIFIED,
        )
        ph = ",".join("?" * len(keep))
        with self._lock:
            rows = self.conn.execute(
                f"SELECT shortcode FROM tasks WHERE state IN ({ph})", keep
            ).fetchall()
        return {r["shortcode"] for r in rows}

    def failed(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM tasks WHERE state=?", (State.FAILED,)).fetchall()
        return [dict(r) for r in rows]

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()

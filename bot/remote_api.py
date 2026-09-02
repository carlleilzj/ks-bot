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
  GET  /api/health             健康检查（token 可选，用于 worker 心跳）
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
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
        """待发布清单：SUBTITLED/READY 且 final 文件在，任务的目标平台还剩 PENDING job。"""
        out = []
        for task in self.db.pending(limit=100):
            if task["state"] not in (State.SUBTITLED, State.READY):
                continue
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

    def claim(self, task_id: int, platform: str) -> dict:
        """原子认领：UPDATE ... WHERE state='PENDING' 抢占，防两端并发重复发布。"""
        with self.db._lock:
            cur = self.db.conn.execute(
                "UPDATE publish_jobs SET state=?, updated_at=? "
                "WHERE task_id=? AND platform=? AND state='PENDING'",
                (PUBLISHING, now_iso(), task_id, platform))
            self.db.conn.commit()
            claimed = cur.rowcount > 0
        if not claimed:
            return {"ok": False, "error": "job 不在 PENDING 状态（已被认领或已完成）"}
        task = self.db.get(task_id)
        if not task:
            return {"ok": False, "error": "任务不存在"}
        return {"ok": True, "task": self._task_payload(task)}

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
        """发布结果回报。ok=True → job PUBLISHED + 通知；LoginExpired → SKIPPED；
        其他失败 → 回 PENDING 等 worker 重试（不消耗 gate，retries+1）。"""
        task_id = int(data.get("task_id") or 0)
        platform = str(data.get("platform") or "").strip()
        if not task_id or not platform:
            return {"ok": False, "error": "task_id/platform 必填"}
        ok = bool(data.get("ok"))
        err = str(data.get("error") or "")[:500]
        login_expired = bool(data.get("login_expired"))
        url = str(data.get("url") or "")[:500]

        with self.db._lock:
            cur = self.db.conn.execute(
                "UPDATE publish_jobs SET state=?, url=?, error=?, retries=CASE WHEN ? THEN retries ELSE retries+1 END, "
                "published_at=? WHERE task_id=? AND platform=? AND state=?",
                (JobState.PUBLISHED if ok else (JobState.SKIPPED if login_expired else JobState.PENDING),
                 url if ok else None, err if not ok else None,
                 1 if ok or login_expired else 0,
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
        if ok:
            log.info("[%s] %s 发布成功：%s", task["shortcode"], display, url or "-")
            telegram.notify_info(self.s, f"✅ [{task['shortcode']}] {display} 发布成功（远程发布端回报）"
                                         + (f"\n链接：{url}" if url else ""))
        elif login_expired:
            log.warning("[%s] %s 登录态失效，job 已 SKIPPED", task["shortcode"], display)
            telegram.notify_info(self.s, f"🔑 [{task['shortcode']}] {display} 登录态失效，job 已跳过\n"
                                         f"请到发布端运行 python -m bot.main --login {platform} 重新扫码")
        else:
            log.warning("[%s] %s 发布失败：%s", task["shortcode"], display, err[:150])
            telegram.notify_info(self.s, f"⚠️ [{task['shortcode']}] {display} 发布失败，将自动重试\n错误：{err[:200]}")

        # 全部 job 终态 → 任务 PUBLISHED
        with self.db._lock:
            cur2 = self.db.conn.execute(
                "SELECT COUNT(*) c FROM publish_jobs WHERE task_id=? AND state NOT IN ('PUBLISHED','SKIPPED','FAILED')",
                (task_id,))
            remaining = cur2.fetchone()[0]
        if remaining == 0:
            self.db.update(task_id, state=State.PUBLISHED, published_at=now_iso(), error=None)
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
                self._send(200, p.read_bytes(), content_type="application/octet-stream")

            def do_POST(self):
                parsed = urlparse(self.path)
                if not self._authed():
                    self._send_json(401, {"error": "unauthorized"})
                    return
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 1 << 20)
                    data = json.loads(self.rfile.read(length) or b"{}")
                except Exception:
                    self._send_json(400, {"error": "invalid json"})
                    return
                if parsed.path == "/api/claim":
                    try:
                        self._send_json(200, api.claim(int(data.get("task_id") or 0),
                                                       str(data.get("platform") or "")))
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
        # 绑定安全校验：只允许 Tailscale CGNAT（100.64.0.0/10）、其他私有网段和回环
        # 注意：100.64/10 是运营商级 NAT 保留段（Tailscale 用），Python 的 is_private=False
        ip = ipaddress.ip_address(host)
        ts_net = ipaddress.ip_network("100.64.0.0/10")
        if not (ip in ts_net or ip.is_private or ip.is_loopback):
            raise RemoteApiError(
                f"拒绝绑定非私有地址 {host}：API 只能绑 Tailscale IP（100.x/CGNAT）或 127.0.0.1")

        server = ThreadingHTTPServer((host, port), Handler)
        self.server = server  # 暴露给测试/管理代码（读 server.server_address[1] 取实际端口）
        log.info("Remote API 监听 http://%s:%d（Tailscale 内网）",
                 host, server.server_address[1])
        server.serve_forever()


def start_api_thread(s: Settings, db: Database, gate_fn) -> threading.Thread:
    """起一个 daemon 线程跑 API 服务；bind/port 从 Settings.remote_api_bind/port 读。"""
    api = RemoteApi(s, db, gate_fn=gate_fn)
    t = threading.Thread(
        target=api.serve_forever, args=(s.remote_api_bind, s.remote_api_port),
        name="remote-api", daemon=True)
    t.start()
    return t

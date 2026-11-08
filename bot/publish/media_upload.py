"""发布媒体公网托管：把成品视频临时暴露成公网 URL，供 Meta 服务器拉取。

为什么需要它：
    Instagram / Facebook 的 Reels API **不接受本地文件上传**，只接受 video_url，
    Meta 的服务器会自己 cURL 这个 URL 拉视频。所以必须先让视频能从公网访问。

设计原则（安全 + 可清理）：
1. **只暴露成品目录的特定文件**：不是把整个 media 目录挂出去，而是通过 token
   路径前缀映射到单条任务，路径穿越（../）直接拒绝；
2. **一次性短期有效**：每个 token 对应一个文件、带过期时间，发布完由清理任务删除；
3. **不依赖外网端口**：复用协调端已有的 nginx（80/443），加一个 location 反代到本进程；
4. **带签名校验**：URL 里带 HMAC 签名，防止被枚举猜测（作品内容不应被随机访问）。

实现方式：起一个极小的 ThreadingHTTPServer（只处理 GET），
    /ig-media/<token>/<filename>
token 是 HMAC(secret, path+bucket_expiry) 的前缀，过期即 403。
nginx 只把 /ig-media/ 前缀反代进来，其余路径照旧。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ..config import FINAL_DIR

log = logging.getLogger(__name__)

# 媒体访问 token 有效期（秒）：够 Meta 拉取 + 处理即可，默认 2 小时
MEDIA_URL_TTL_SEC = 2 * 3600

_MEDIA_PREFIX = "/ig-media/"


def _secret() -> str:
    """签名密钥。优先用 env，缺失时退回 remote_api_token（两条通道都是服务端私有）。"""
    return (os.getenv("IG_MEDIA_SECRET") or os.getenv("REMOTE_API_TOKEN") or "ksbot-media").strip()


def sign_path(rel_path: str, expiry: int) -> str:
    """生成 URL 内的签名片段。"""
    msg = f"{rel_path}|{expiry}".encode()
    return hmac.new(_secret().encode(), msg, hashlib.sha256).hexdigest()[:32]


def build_media_token(rel_path: str, ttl: int = MEDIA_URL_TTL_SEC) -> str:
    """构造 <签名>.<过期时间戳> 形式的 token。"""
    expiry = int(time.time()) + max(60, ttl)
    sig = sign_path(rel_path, expiry)
    return f"{sig}.{expiry}"


def verify_media_token(rel_path: str, token: str) -> bool:
    """校验 token 签名与有效期。"""
    try:
        sig, exp_s = token.rsplit(".", 1)
        expiry = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if expiry < int(time.time()):
        return False
    expected = sign_path(rel_path, expiry)
    return hmac.compare_digest(sig, expected)


def public_base_url() -> str:
    """媒体对外的基地址（Meta 能访问到的公网域名）。"""
    return (os.getenv("IG_MEDIA_BASE_URL") or "").strip().rstrip("/")


def build_public_url(local_path: Path, base_url: str = "", ttl: int = MEDIA_URL_TTL_SEC) -> str:
    """把一个本地成品视频转成公网可访问 URL。

    local_path 必须在 FINAL_DIR 之内（白名单），否则拒绝——避免任意文件外泄。
    """
    base = (base_url or public_base_url()).rstrip("/")
    if not base:
        raise RuntimeError(
            "未配置 IG_MEDIA_BASE_URL：Meta 需要公网可访问的 video_url，"
            "请在 .env 设置（如 https://media.example.com）")

    p = Path(local_path).resolve()
    final_root = FINAL_DIR.resolve()
    if not p.is_relative_to(final_root):
        raise RuntimeError(f"拒绝暴露成品目录之外的文件：{p}")
    if not p.is_file():
        raise RuntimeError(f"媒体文件不存在：{p}")

    rel = str(p.relative_to(final_root))
    token = build_media_token(rel, ttl=ttl)
    return f"{base}{_MEDIA_PREFIX}{token}/{rel}"


# ---------------------------------------------------------------------------
# 媒体服务（只读 GET，nginx 反代 /ig-media/ 到本进程）
# ---------------------------------------------------------------------------

class _MediaHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith(_MEDIA_PREFIX):
            self._plain(404, "not found")
            return

        rest = path[len(_MEDIA_PREFIX):]
        if "/" not in rest:
            self._plain(400, "bad token")
            return
        token, rel = rest.split("/", 1)

        if not verify_media_token(rel, token):
            log.warning("媒体签名校验失败或已过期：%s", rel[:80])
            self._plain(403, "forbidden")
            return

        # 路径穿越防护：只允许 FINAL_DIR 内的文件
        final_root = FINAL_DIR.resolve()
        target = (final_root / rel).resolve()
        if not target.is_relative_to(final_root):
            self._plain(403, "forbidden")
            return
        if not target.is_file():
            self._plain(404, "not found")
            return

        try:
            size = target.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "none")
            self.end_headers()
            with open(target, "rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)
            log.info("媒体已投递：%s（%.1f MB）", target.name, size / 1048576)
        except (BrokenPipeError, ConnectionResetError):
            log.debug("媒体投递被对端中断（Meta 常常拉完即断，正常）：%s", target.name)
        except Exception as e:
            log.warning("媒体投递失败：%s", str(e)[:150])
            try:
                self._plain(500, "internal error")
            except Exception:
                pass

    def _plain(self, code: int, msg: str) -> None:
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass


_media_server: ThreadingHTTPServer | None = None


def start_media_server(bind_host: str = "127.0.0.1", port: int = 8766) -> threading.Thread:
    """起媒体服务（daemon 线程）。幂等：重复调用返回同一个。"""
    global _media_server
    if _media_server is not None:
        log.debug("媒体服务已在运行，跳过重复启动")
        return None  # type: ignore[return-value]

    server = ThreadingHTTPServer((bind_host, port), _MediaHandler)
    _media_server = server
    t = threading.Thread(target=server.serve_forever, name="ig-media", daemon=True)
    t.start()
    log.info("IG 媒体服务监听 http://%s:%d%s（nginx 需反代该路径）",
             bind_host, port, _MEDIA_PREFIX)
    return t

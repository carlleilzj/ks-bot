"""Instagram 解析器：绕过 yt-dlp 的失效提取器，直连移动端 API。

背景
----
yt-dlp 2026.08.19 的 Instagram extractor 走 `www.instagram.com/api/graphql`
（doc_id 27130156389949648）。IG 已废弃该端点，实测恒返回：
    HTTP 400  {"message":"useragent mismatch"} / {"message":"Media not found"}
导致所有 IG 链接解析失败。

本模块改用 IG 移动端 API（实测可用）：
    1. shortcode → media pk（IG 自定义 64 进制）
    2. GET i.instagram.com/api/v1/media/<pk>/info/   取元数据 + 视频直链
    3. 缺失时回退 www.instagram.com/api/v1（web app id）

账号轮换
--------
cookie 从 ig_pool 取，命中风控时自动冷却该账号并换下一个。

对外接口
--------
    extract(url) -> VideoMeta      与 bot.source.downloader.VideoMeta 同构
    is_instagram(url) -> bool
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from . import ig_pool

log = logging.getLogger(__name__)

# shortcode 使用的自定义 64 进制字符表
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

# 移动端 UA：IG 对浏览器 UA 会走 web 端点（已废弃），必须伪装移动客户端
_MOBILE_UA = ("Instagram 361.0.0.36.91 (iPhone15,2; iOS 18_1; en_US; en-US; "
              "scale=3.00; 1290x2796; 623744990)")

_APP_ID_IOS = "124024574287414"   # → i.instagram.com/api/v1
_APP_ID_WEB = "936619743392459"   # → www.instagram.com/api/v1

_TIMEOUT = 30


def is_instagram(url: str) -> bool:
    return "instagram.com" in (url or "").lower()


def shortcode_to_pk(shortcode: str) -> int:
    """IG shortcode → media pk（自定义 64 进制）。"""
    n = 0
    for ch in shortcode:
        idx = _ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"非法 shortcode 字符：{ch!r}")
        n = n * 64 + idx
    return n


def _extract_shortcode(url: str) -> str:
    """从各种 IG 链接形态里取 shortcode。

    支持：
        /reel/<sc>/   /reels/<sc>/   /p/<sc>/   /tv/<sc>/
        /<username>/reel/<sc>/
    """
    from urllib.parse import urlparse
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    for i, p in enumerate(parts):
        if p in ("reel", "reels", "p", "tv") and i + 1 < len(parts):
            return parts[i + 1]
    # 兜底：最后一段像 shortcode 的
    if parts and 8 <= len(parts[-1]) <= 15:
        return parts[-1]
    raise ValueError(f"无法从链接里提取 shortcode：{url}")


def _read_cookie(path: Path) -> dict[str, str]:
    """读 Netscape cookie 文件 → dict。"""
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            # 同上：`#HttpOnly_` 是 Netscape 格式对 HttpOnly cookie 的标记前缀
            line = line.removeprefix("#HttpOnly_")
            if not line.strip() or line.startswith("#"):
                continue
            f = line.split("\t")
            if len(f) >= 7:
                out[f[5]] = f[6]
    except Exception as e:
        log.warning("读 cookie 失败 %s：%s", path, e)
    return out


def _try_fetch(pk: int, cookies: dict, app_id: str, host: str) -> tuple[dict | None, int]:
    """请求 media info。返回 (items[0] 或 None, HTTP 状态码)。

    返回状态码是为了区分「帖子真的没了（404）」和「登录态失效（401/403）/限流（429）」。
    此前一律返回 None，会话过期被降级成「帖子不可访问」，账号永不冷却，
    于是每轮都优先选这个坏账号，形成稳定失败循环。
    """
    url = f"https://{host}/api/v1/media/{pk}/info/"
    headers = {
        "User-Agent": _MOBILE_UA,
        "X-IG-App-ID": app_id,
        "X-IG-Capabilities": "3brTvwE=",
        "X-IG-Connection-Type": "WIFI",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    }
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as c:
            r = c.get(url, headers=headers)
    except Exception as e:
        log.debug("media info 请求异常（%s）：%s", host, e)
        return None, 0

    if r.status_code != 200:
        log.debug("media info %s HTTP %d: %s", host, r.status_code, r.text[:150])
        return None, r.status_code
    try:
        d = r.json()
    except Exception:
        # 被重定向到登录/挑战页时返回的是 HTML（r.json() 失败）。
        # 必须返回 401 而不是 0：调用方只把 401/403/429 当会话失效，
        # 返回 0 会落进「帖子不可访问」分支，坏账号照样不被冷却 —— 等于没修。
        log.debug("media info %s 返回非 JSON（疑似被重定向到登录/挑战页）", host)
        return None, 401

    # IG 的另一种「会话失效」形态：HTTP 200 + 失败体
    # （{"message": "login_required"/"checkpoint_required", "status": "fail"}）。
    # 只看状态码会漏判，坏账号照样不会被冷却。但要区分「帖子没了」和「登录坏了」，
    # 否则删帖也会冤枉账号进冷却。
    if str(d.get("status") or "").lower() == "fail" or d.get("require_login"):
        msg = str(d.get("message") or "").lower()
        log.debug("media info %s 返回失败体：%s", host, msg[:120])
        if "login" in msg or "checkpoint" in msg or "challenge" in msg or d.get("require_login"):
            return None, 401
        if "not found" in msg or "unavailable" in msg or "deleted" in msg:
            return None, 404
        # 无法归类：不冷却账号（宁可漏判也不要误伤健康账号），
        # 返回 0 走「帖子不可访问」分支。
        return None, 0

    items = d.get("items") or []
    return (items[0] if items else None), r.status_code


def _best_video_url(item: dict) -> str:
    """从 item 里挑最高清的视频直链（含 carousel 轮播里的视频）。"""
    vv = item.get("video_versions") or []
    if vv:
        # 按带宽/尺寸降序
        def key(v):
            return (v.get("width", 0) * v.get("height", 0), v.get("bandwidth", 0))
        return sorted(vv, key=key, reverse=True)[0].get("url", "")

    # 检查 carousel_media（轮播合集里可能包含视频）
    for sub in (item.get("carousel_media") or []):
        sub_vv = sub.get("video_versions") or []
        if sub_vv:
            def key(v):
                return (v.get("width", 0) * v.get("height", 0), v.get("bandwidth", 0))
            return sorted(sub_vv, key=key, reverse=True)[0].get("url", "")

    return ""


def _best_image_url(item: dict) -> str:
    """图文帖取第一张（含 carousel）。"""
    if item.get("carousel_media"):
        first = item["carousel_media"][0]
        return _best_image_url(first)
    cands = (item.get("image_versions2") or {}).get("candidates") or []
    if not cands:
        return ""
    return sorted(cands, key=lambda c: c.get("width", 0) * c.get("height", 0),
                  reverse=True)[0].get("url", "")


def extract(url: str):
    """解析 IG 链接 → VideoMeta。失败抛 ValueError。"""
    from .downloader import VideoMeta, parse_url

    clean_url = parse_url(url)
    shortcode = _extract_shortcode(clean_url)
    try:
        pk = shortcode_to_pk(shortcode)
    except ValueError as e:
        raise ValueError(str(e)) from e

    cf = ig_pool.pick_cookiefile()
    if cf is None:
        raise ValueError("IG 账号池为空且无可用 cookie，无法解析")
    acct = ig_pool.account_of(cf)
    cookies = _read_cookie(cf)
    if "sessionid" not in cookies:
        raise ValueError(f"cookie 文件缺少 sessionid：{cf}")

    # 依次尝试：iOS 端点 → Web 端点
    item = None
    last_err = ""
    auth_failed = False
    for host, app_id in (("i.instagram.com", _APP_ID_IOS),
                         ("www.instagram.com", _APP_ID_WEB)):
        item, status = _try_fetch(pk, cookies, app_id, host)
        if item:
            log.info("IG 解析成功：%s（%s，账号 %s）", shortcode, host, acct)
            break
        # 401/403 = 登录态失效；429 = 限流。这两种必须冷却账号，
        # 否则坏账号会被反复优先选中（每轮都失败，运维看到的是「帖子删了」）。
        if status in (401, 403, 429):
            auth_failed = True
        last_err = f"{host} HTTP {status or '异常'}"

    if not item:
        if auth_failed and acct:
            ig_pool.report_blocked(acct, f"IG 会话失效或限流（{last_err}）")
            raise ValueError(
                f"IG 账号 {acct} 会话失效/被限流，已冷却并切换其他账号。"
                f"请到发布端重新扫码登录。详情：{last_err}"
            )
        # 两个端点都没数据：绝大多数是帖子本身已删除/私密。
        raise ValueError(
            f"IG 帖子不可访问（{shortcode}）。可能已被删除、设为私密，"
            f"或需要登录。详情：{last_err}"
        )

    if acct:
        ig_pool.report_ok(acct)

    # 组装 VideoMeta
    user = item.get("user") or {}
    username = user.get("username", "")
    caption = (item.get("caption") or {}).get("text", "") or ""
    video_url = _best_video_url(item)
    image_url = _best_image_url(item)
    media_type = item.get("media_type")  # 1=图 2=视频 8=carousel
    duration = float(item.get("video_duration") or 0)
    if not duration and item.get("carousel_media"):
        for sub in item["carousel_media"]:
            if sub.get("video_duration"):
                duration = float(sub["video_duration"])
                break

    if not video_url:
        raise ValueError(f"该 IG 链接无视频内容（纯图文或静态图片轮播：{shortcode}）")

    taken_at = item.get("taken_at")
    from datetime import datetime, timezone
    published = ""
    if taken_at:
        published = datetime.fromtimestamp(taken_at, tz=timezone.utc).isoformat()

    return VideoMeta(
        source_url=clean_url,
        platform="instagram",
        video_id=shortcode,
        shortcode=f"instagram_{shortcode}",
        username=username,
        title=caption.split("\n")[0][:120] if caption else "",
        caption=caption,
        thumbnail_url=image_url,
        duration=duration,
        permalink=f"https://www.instagram.com/reel/{shortcode}/",
    )


def download(url: str, dest: Path) -> Path:
    """直接下载 IG 高清原片（秒级直链下载，绕过 yt-dlp 门禁与格式问题）。"""
    from .downloader import parse_url

    dest.parent.mkdir(parents=True, exist_ok=True)
    clean_url = parse_url(url)
    shortcode = _extract_shortcode(clean_url)
    pk = shortcode_to_pk(shortcode)

    cf = ig_pool.pick_cookiefile()
    cookies = _read_cookie(cf) if cf else {}

    item = None
    for host, app_id in (("i.instagram.com", _APP_ID_IOS),
                         ("www.instagram.com", _APP_ID_WEB)):
        item, status = _try_fetch(pk, cookies, app_id, host)
        if item:
            break

    if not item:
        raise ValueError(f"获取 IG 帖子详情失败：{shortcode}")

    video_url = _best_video_url(item)
    if not video_url:
        raise ValueError(f"该 IG 链接无视频可下载（纯图文或静态图片轮播：{shortcode}）")

    log.info("[instagram] 开始直链下载原画视频: %s -> %s", shortcode, dest.name)
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15",
    }
    with httpx.Client(timeout=120, follow_redirects=True, headers=headers) as client:
        with client.stream("GET", video_url) as resp:
            if resp.status_code != 200:
                raise ValueError(f"下载 IG 直链失败 HTTP {resp.status_code}: {video_url[:100]}")
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

    if not dest.exists() or dest.stat().st_size == 0:
        raise FileNotFoundError(f"IG 下载完成但文件为空: {dest}")

    size_mb = dest.stat().st_size / (1024 * 1024)
    log.info("[instagram] 下载成功: %s (%.1f MB)", dest.name, size_mb)
    return dest


def probe(cookiefile: Path | None = None) -> dict:
    """连通性自检：验证 cookie 能否拉到数据。"""
    cf = cookiefile or ig_pool.pick_cookiefile()
    if cf is None:
        return {"ok": False, "reason": "无可用 cookie"}
    cookies = _read_cookie(cf)
    if "sessionid" not in cookies:
        return {"ok": False, "reason": "cookie 缺 sessionid"}
    headers = {
        "User-Agent": _MOBILE_UA,
        "X-IG-App-ID": _APP_ID_IOS,
        "X-IG-Capabilities": "3brTvwE=",
        "X-IG-Connection-Type": "WIFI",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    }
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as c:
            r = c.get("https://i.instagram.com/api/v1/users/web_profile_info/",
                      params={"username": "instagram"}, headers=headers)
            txt = r.text
        try:
            d = json.loads(txt)
        except Exception:
            d = {}
        ok = r.status_code == 200 and bool((d.get("data") or {}).get("user"))
        return {
            "ok": ok,
            "account": ig_pool.account_of(cf),
            "cookiefile": str(cf),
            "http": r.status_code,
            "reason": "" if ok else (str(d)[:200] or txt[:200]),
        }
    except Exception as e:
        return {"ok": False, "reason": f"请求异常：{e}"}

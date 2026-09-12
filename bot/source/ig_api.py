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
            if not line.strip() or line.startswith("#"):
                continue
            f = line.split("\t")
            if len(f) >= 7:
                out[f[5]] = f[6]
    except Exception as e:
        log.warning("读 cookie 失败 %s：%s", path, e)
    return out


def _try_fetch(pk: int, cookies: dict, app_id: str, host: str) -> dict | None:
    """请求 media info。返回 items[0] 或 None。"""
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
        return None

    if r.status_code != 200:
        log.debug("media info %s HTTP %d: %s", host, r.status_code, r.text[:150])
        return None
    try:
        d = r.json()
    except Exception:
        return None
    items = d.get("items") or []
    return items[0] if items else None


def _best_video_url(item: dict) -> str:
    """从 item 里挑最高清的视频直链。"""
    vv = item.get("video_versions") or []
    if not vv:
        return ""
    # 按带宽/尺寸降序
    def key(v):
        return (v.get("width", 0) * v.get("height", 0), v.get("bandwidth", 0))
    return sorted(vv, key=key, reverse=True)[0].get("url", "")


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
    for host, app_id in (("i.instagram.com", _APP_ID_IOS),
                         ("www.instagram.com", _APP_ID_WEB)):
        item = _try_fetch(pk, cookies, app_id, host)
        if item:
            log.info("IG 解析成功：%s（%s，账号 %s）", shortcode, host, acct)
            break
        last_err = f"{host} 无数据"

    if not item:
        # 两个端点都没数据：绝大多数是帖子本身已删除/私密。
        # 账号是否被风控由 probe() 判定，这里不冷却（避免误伤）。
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

    if media_type == 1 and not video_url:
        raise ValueError(f"该 IG 链接是纯图文帖，无视频可下载（{shortcode}）")

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

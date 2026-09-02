"""（遗留）Instagram 监控：用 instaloader 拉取目标公开账号的 Reels。

主循环已改为 TG 投链驱动，本模块不再被 bot.main 调用。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import instaloader

from ..config import RAW_DIR, Settings

log = logging.getLogger(__name__)

# instaloader 会话文件缓存路径（登录一次后复用，避免反复登录触发风控）
SESSION_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "ig_session"
# 用户名 -> user_id 持久缓存（映射不变，避免每轮都调 topsearch，把 API 调用砍半）
USER_ID_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "ig_user_ids.json"


def _load_uid_cache() -> dict:
    try:
        return json.loads(USER_ID_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_uid_cache(cache: dict) -> None:
    try:
        USER_ID_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log.debug("写 user_id 缓存失败：%s", e)


class InstagramError(RuntimeError):
    pass


class InstagramCheckpoint(InstagramError):
    """账号被 IG 风控锁定（checkpoint_required），需要人工解锁后才能继续。"""


class InstagramSoftLock(InstagramError):
    """临时软限流（401 require_login / Please wait）：等待即可恢复，继续请求只会延长封锁。"""


@dataclass
class IgPost:
    """一条 IG 作品的标准化信息。"""
    media_id: str          # instaloader 的 mediaid（数字字符串）
    shortcode: str
    username: str
    caption: str
    media_type: str        # VIDEO / IMAGE / SIDECAR
    permalink: str
    timestamp: float       # 发布时间（unix）
    video_url: str         # 视频直链（instaloader 内部 url）
    thumbnail_url: str


def _raise_http(r, action: str) -> None:
    """非 200 统一分类：checkpoint/login 锁定抛专用异常，其余带状态码。"""
    body = ""
    try:
        body = r.text[:300]
    except Exception:
        pass
    if "checkpoint_required" in body or "login_required" in body:
        raise InstagramCheckpoint(
            f"IG 账号被风控锁定（HTTP {r.status_code}），需要人工解锁后重新登录")
    if r.status_code == 401 and ("Please wait" in body or '"require_login"' in body):
        raise InstagramSoftLock(
            f"IG 临时软限流（HTTP 401 {body[:80]}），等待自动恢复，期间停止请求")
    raise InstagramError(f"{action}失败: HTTP {r.status_code} {body[:120]}")


class InstagramClient:
    """instaloader 封装：登录态管理 + 拉取目标账号最新 Reels。"""

    def __init__(self, s: Settings):
        self.s = s
        if not s.ig_targets:
            raise InstagramError("缺少 IG_TARGETS：请在 .env 填写要监控的目标账号用户名")
        if not s.ig_login_user:
            raise InstagramError("缺少 IG_LOGIN_USER：请填写你的 Instagram 小号用户名（用于登录 instaloader）")
        self._L = instaloader.Instaloader(
            download_videos=False,      # 我们自己控制下载
            download_video_thumbnails=False,
            save_metadata=False,
            post_metadata_txt_pattern="",
            quiet=True,
            user_agent=None,            # instaloader 默认 UA
        )
        self._login()

    def _login(self) -> None:
        """优先复用已保存的会话；否则用账号密码登录并保存。"""
        try:
            if SESSION_FILE.exists():
                self._L.load_session_from_file(self.s.ig_login_user, str(SESSION_FILE))
                log.info("已复用 instaloader 会话（用户 %s）", self.s.ig_login_user)
                return
        except Exception as e:
            log.warning("会话文件加载失败，将重新登录：%s", e)

        if not self.s.ig_login_pass:
            raise InstagramError("缺少 IG_LOGIN_PASS：首次登录需要密码；登录成功后会话会缓存")
        try:
            self._L.login(self.s.ig_login_user, self.s.ig_login_pass)
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._L.save_session_to_file(str(SESSION_FILE))
            log.info("instaloader 登录成功，会话已缓存到 %s", SESSION_FILE)
        except instaloader.exceptions.TwoFactorAuthRequiredException:
            raise InstagramError(
                "你的 IG 小号开启了二次验证。请在 IG 设置里临时关闭 2FA，"
                "或换一个没开 2FA 的小号（instaloader 不支持 2FA 自动输入）"
            )
        except instaloader.exceptions.BadCredentialsException as e:
            raise InstagramError(f"IG 小号登录失败（密码错误）：{e}") from e
        except instaloader.exceptions.LoginRequiredException as e:
            raise InstagramError(f"IG 登录失败：{e}") from e
        except Exception as e:
            raise InstagramError(f"IG 登录异常：{e}") from e

    def fetch_media(self, limit: int = 15) -> list[IgPost]:
        """拉取所有目标账号的最新作品（合并、按时间倒序），返回 IgPost 列表。

        只返回 VIDEO 类型（Reels）。limit 是每个账号拉取的条数。
        """
        all_posts: list[IgPost] = []
        failed = 0
        for target in self.s.ig_targets:
            try:
                posts = self._fetch_profile(target, limit)
                all_posts.extend(posts)
                log.info("目标 %s：拉取到 %d 条视频作品", target, len(posts))
            except (InstagramCheckpoint, InstagramSoftLock):
                # 锁定/限流状态下继续请求只会延长封锁，立即放弃本轮
                raise
            except instaloader.exceptions.ProfileNotExistsException:
                log.warning("目标账号 %s 不存在（用户名拼错了？）", target)
            except Exception as e:
                failed += 1
                log.error("拉取目标 %s 失败：%s", target, e)
        if failed and failed == len(self.s.ig_targets):
            # 全部目标失败 = 会话/风控问题，抛出让上层发 TG 告警
            raise InstagramError(f"全部 {failed} 个目标拉取失败（会话过期或 IG 风控，"
                                 f"请运行 python -m bot.ig_login {self.s.ig_login_user} '密码' 重新验证")
        # 按时间倒序
        all_posts.sort(key=lambda p: p.timestamp, reverse=True)
        return all_posts

    def _fetch_profile(self, target: str, limit: int) -> list[IgPost]:
        """拉取目标账号的视频作品。直接用轻量 API，绕过 instaloader 的 from_username（会被 429 卡住）。"""
        return self._fetch_via_feed_api(target, limit)

    def _fetch_via_feed_api(self, target: str, limit: int) -> list[IgPost]:
        """用 IG 的 /api/v1/feed/user/ 端点拉取作品（限流宽松）。"""
        import httpx
        # 先获取用户 ID（用搜索 API，比 web_profile_info 轻量）
        cookies = {}
        for ck in self._L.context._session.cookies:
            cookies[ck.name] = ck.value
        csrftoken = cookies.get("csrftoken", "")
        headers = {
            "User-Agent": self._L.context.user_agent,
            "X-IG-App-ID": "936619743392459",
            "X-CSRFToken": csrftoken,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.instagram.com/{target}/",
            "Accept": "*/*",
        }
        with httpx.Client(cookies=cookies, headers=headers, timeout=30, follow_redirects=True) as c:
            # 先查持久缓存，命中就跳过 topsearch（映射不变，省一半 API 调用）
            cache = _load_uid_cache()
            user_id = cache.get(target)
            if not user_id:
                r = c.get("https://www.instagram.com/api/v1/web/search/topsearch/",
                          params={"context": "blended", "query": target, "include_reel": "false"})
                if r.status_code != 200:
                    _raise_http(r, "搜索用户")
                users = r.json().get("users", [])
                for u in users:
                    if u.get("user", {}).get("username") == target:
                        user_id = u["user"]["pk"]
                        break
                if not user_id:
                    raise InstagramError(f"未找到用户 @{target}")
                cache[target] = user_id
                _save_uid_cache(cache)

            # 拉取用户 feed
            r2 = c.get(f"https://www.instagram.com/api/v1/feed/user/{user_id}/",
                        params={"count": limit})
            if r2.status_code != 200:
                _raise_http(r2, "拉取 feed")
            items = r2.json().get("items", [])
            out: list[IgPost] = []
            for item in items:
                if item.get("media_type") != 2:  # 2 = VIDEO
                    continue
                shortcode = item.get("code", "")
                caption = (item.get("caption") or {}).get("text", "")
                video_url = ""
                versions = item.get("video_versions") or []
                if versions:
                    video_url = versions[0].get("url", "")
                thumbnail = item.get("thumbnail_url") or ""
                out.append(IgPost(
                    media_id=str(item.get("pk", shortcode)),
                    shortcode=shortcode,
                    username=target,
                    caption=caption,
                    media_type="VIDEO",
                    permalink=f"https://www.instagram.com/p/{shortcode}/",
                    timestamp=float(item.get("taken_at", 0)),
                    video_url=video_url,
                    thumbnail_url=thumbnail,
                ))
            return out

    def _post_to_igpost(self, post, target: str) -> IgPost:
        """instaloader Post -> IgPost。"""
        return IgPost(
            media_id=str(post.mediaid),
            shortcode=post.shortcode,
            username=post.owner_username or target,
            caption=post.caption or "",
            media_type="VIDEO",
            permalink=f"https://www.instagram.com/p/{post.shortcode}/",
            timestamp=post.date_utc.replace(tzinfo=None).timestamp(),
            video_url=post.video_url,
            thumbnail_url=post.url,
        )

    def download(self, post: IgPost, dest: Path) -> Path:
        """下载视频到指定路径。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            return dest
        # 用 instaloader 的 context 下载视频（处理 IG 的 CDN 直链 + cookie）
        try:
            # 直接用 video_url 通过 instaloader 的下载器（带 cookie）
            from instaloader.downloader import _download_url as _dl
        except ImportError:
            _dl = None

        # instaloader 内部下载函数签名不稳定，这里用 httpx 复用 context 的 cookie
        import httpx
        cookies = {}
        # instaloader context 的 cookie 存储
        for ck in self._L.context._session.cookies:
            cookies[ck.name] = ck.value
        headers = {
            "User-Agent": self._L.context.user_agent,
            "Referer": "https://www.instagram.com/",
        }
        with httpx.Client(timeout=120, follow_redirects=True, cookies=cookies, headers=headers) as c:
            r = c.get(post.video_url)
            if r.status_code != 200:
                raise InstagramError(f"下载视频失败（HTTP {r.status_code}）：{post.shortcode}")
            dest.write_bytes(r.content)
        size_mb = dest.stat().st_size / 1024 / 1024
        log.info("已下载 %s（%.1f MB）", dest.name, size_mb)
        return dest

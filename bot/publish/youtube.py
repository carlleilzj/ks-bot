"""YouTube Shorts 发布（YouTube Data API v3 官方官方通道）。

与 Instagram 一样走海外官方 API 协议：
1. **纯 HTTP / 无需浏览器**：不依赖 Playwright，机房海外 IP 直接调用 Google API；
2. **长效 OAuth2**：通过 Client ID + Client Secret + Refresh Token 自动换取 Access Token，永不过期；
3. **支持断点续传**：使用 Google Resumable Upload 协议上传成品视频；
4. **配额安全**：免费项目每日 10,000 units 配额，单条视频上传 1,600 units，日限 2~4 条完全处于安全区间。

环境变量配置（.env）：
    YOUTUBE_CLIENT_ID
    YOUTUBE_CLIENT_SECRET
    YOUTUBE_REFRESH_TOKEN
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from .base import LoginExpired, PublishError

log = logging.getLogger(__name__)

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

CATEGORY_FILM_ANIMATION = "1"  # Film & Animation 分区


class YouTubeError(PublishError):
    pass


@dataclass(frozen=True)
class YouTubeAccount:
    client_id: str
    client_secret: str
    refresh_token: str

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)


def load_account() -> YouTubeAccount | None:
    cid = (os.getenv("YOUTUBE_CLIENT_ID") or "").strip()
    secret = (os.getenv("YOUTUBE_CLIENT_SECRET") or "").strip()
    refresh = (os.getenv("YOUTUBE_REFRESH_TOKEN") or "").strip()
    if not (cid and secret and refresh):
        return None
    return YouTubeAccount(client_id=cid, client_secret=secret, refresh_token=refresh)


def get_access_token(acct: YouTubeAccount, timeout: float = 30.0) -> str:
    """使用 refresh_token 换取短期 access_token（有效期 1 小时）。"""
    data = {
        "client_id": acct.client_id,
        "client_secret": acct.client_secret,
        "refresh_token": acct.refresh_token,
        "grant_type": "refresh_token",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(OAUTH_TOKEN_URL, data=data)
            if resp.status_code == 400 or resp.status_code == 401:
                raise LoginExpired(f"YouTube OAuth 凭据失效，需重新授权：{resp.text[:200]}")
            if resp.status_code != 200:
                raise YouTubeError(f"获取 YouTube access_token 失败 (HTTP {resp.status_code}): {resp.text[:200]}")
            tokens = resp.json()
            return tokens.get("access_token") or ""
    except (LoginExpired, YouTubeError):
        raise
    except Exception as e:
        raise YouTubeError(f"请求 Google OAuth2 失败：{e}") from e


def compose_description(title: str, description: str, tags: list[str]) -> str:
    """组合适合 Shorts 的描述文案。"""
    lines = []
    desc_clean = (description or "").strip()
    if desc_clean and desc_clean != (title or "").strip():
        lines.append(desc_clean)

    # 规范化 tags
    tag_words = []
    for t in tags or []:
        w = str(t).strip().lstrip("#")
        if w and w not in tag_words:
            tag_words.append(w)

    # 保证 Shorts 专属标签
    if "Shorts" not in tag_words and "shorts" not in tag_words:
        tag_words.insert(0, "Shorts")

    hash_line = " ".join(f"#{w}" for w in tag_words[:15])
    if hash_line:
        lines.append(hash_line)

    return "\n\n".join(lines).strip()


def upload_video(
    acct: YouTubeAccount,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy_status: str = "public",
    timeout: float = 300.0,
) -> str:
    """通过 Resumable Upload 协议上传视频到 YouTube，返回视频 ID。"""
    token = get_access_token(acct)
    file_path = Path(video_path).resolve()
    if not file_path.exists():
        raise YouTubeError(f"待上传视频文件不存在：{file_path}")

    file_size = file_path.stat().st_size
    desc_final = compose_description(title, description, tags)

    # 标题限制 100 字符，Shorts 建议包含 #Shorts
    title_clean = (title or "Untitled").strip()
    if "#Shorts" not in title_clean and "#shorts" not in title_clean:
        if len(title_clean) + 8 <= 100:
            title_clean = f"{title_clean} #Shorts"
        else:
            title_clean = title_clean[:92] + " #Shorts"

    metadata = {
        "snippet": {
            "title": title_clean,
            "description": desc_final,
            "tags": [t.lstrip("#") for t in tags if t][:20],
            "categoryId": CATEGORY_FILM_ANIMATION,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    # Step 1: 发起分块断点上传会话，换取上传 URL
    init_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4",
        "X-Upload-Content-Length": str(file_size),
    }
    params = {"uploadType": "resumable", "part": "snippet,status"}

    with httpx.Client(timeout=60.0) as client:
        init_resp = client.post(
            YOUTUBE_UPLOAD_URL,
            params=params,
            headers=init_headers,
            content=json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
        )
        if init_resp.status_code == 403 and "quotaExceeded" in init_resp.text:
            raise YouTubeError("YouTube 今日 API 上传配额已耗尽 (quotaExceeded)，请明日再试")
        if init_resp.status_code not in (200, 201):
            raise YouTubeError(f"初始化 YouTube 上传会话失败 (HTTP {init_resp.status_code}): {init_resp.text[:300]}")

        upload_url = init_resp.headers.get("Location")
        if not upload_url:
            raise YouTubeError("YouTube 未返回分块上传 Location 地址")

    # Step 2: 上传视频二进制流
    log.info("开始向 YouTube 传输视频流（%.1f MB）：%s", file_size / 1048576, file_path.name)
    with httpx.Client(timeout=timeout) as client:
        with open(file_path, "rb") as f:
            upload_resp = client.put(
                upload_url,
                headers={"Content-Length": str(file_size), "Content-Type": "video/mp4"},
                content=f,
            )

        if upload_resp.status_code not in (200, 201):
            raise YouTubeError(f"YouTube 视频流传输失败 (HTTP {upload_resp.status_code}): {upload_resp.text[:300]}")

        resp_data = upload_resp.json()
        video_id = resp_data.get("id")
        if not video_id:
            raise YouTubeError(f"YouTube 上传完成但未返回 videoId: {resp_data}")
        log.info("YouTube Shorts 发布完成：videoId=%s", video_id)
        return video_id


def publish(
    video: Path,
    title: str,
    description: str,
    tags: list[str],
    category: str | None = None,
    cover: Path | None = None,
    headless: bool = True,
    state_path: Path | None = None,
    *,
    privacy_status: str = "public",
    **_ignored,
) -> str | None:
    """符合 PublisherSpec 接口的发布入口。返回视频链接。"""
    acct = load_account()
    if acct is None:
        raise LoginExpired(
            "未配置 YouTube OAuth 凭据：请在 .env 中填写 YOUTUBE_CLIENT_ID、"
            "YOUTUBE_CLIENT_SECRET 与 YOUTUBE_REFRESH_TOKEN"
        )

    log.info("[youtube] 准备发布 YouTube Shorts: %s", (title or "")[:40])
    video_id = upload_video(
        acct=acct,
        video_path=video,
        title=title,
        description=description,
        tags=tags,
        privacy_status=privacy_status,
    )
    return f"https://www.youtube.com/shorts/{video_id}"


def login_interactive(state_path: Path | None = None) -> bool:
    """检查凭据或输出指引。"""
    acct = load_account()
    if acct and acct.is_configured:
        try:
            token = get_access_token(acct)
            print("✓ YouTube OAuth 凭据有效，成功获取 access_token！")
            return True
        except Exception as e:
            print(f"❌ YouTube 凭据校验失败：{e}")
            return False
    print("⚠️ 请在 .env 中配置 YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN")
    return False

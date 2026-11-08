"""Instagram / Facebook Reels 发布（Meta Graph API 官方通道）。

为什么走官方 API 而不是 Playwright 自动化（与快手/抖音的浏览器方案不同）：
1. **官方支持且稳定**：Meta 为 Reels 提供正式的内容发布 API，不会因页面改版而失效；
2. **无风控**：API 本身面向云端 SaaS 设计，机房 IP 调用完全合规，不像网页端那样动辄触发扫码/人脸核验；
3. **可无头运行**：不需要浏览器、不需要 Playwright、不需要登录态文件。

核心约束（Meta 官方文档，2026-11 核对）：
- **媒体必须在公网可访问的服务器上**（Meta 用自己的服务器 cURL 拉取），
  所以本地视频必须先上传到可公网访问的 URL 上（见 media_upload.upload_for_meta）；
- 只支持 JPEG 图片（本模块只发 Reels 视频，不影响）；
- 单账号 24 小时最多 100 条 API 发布；
- 发布是三步异步流程：
  a. POST /<IG_ID>/media          创建容器（media_type=REELS, video_url, caption）
  b. GET  /<CONTAINER_ID>?fields=status_code   轮询直到 FINISHED
  c. POST /<IG_ID>/media_publish  正式发布

鉴权：这里用「Instagram Login」通道（graph.instagram.com），
因为我们的 4 个账号是手机直接注册的 Instagram 专业号，
没有绑定 Facebook Page，走 Instagram Login 拿 long-lived token 最简。

env 需要的项（按账号）：
    IG_TOKEN_<账号key>     # long-lived access token
    IG_USERID_<账号key>    # Instagram professional account id
账号 key 与 platforms 配置里的平台名一致（如 petmemes / satisfying / anim3d / homesmart）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .base import PublishError

log = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.instagram.com"
# Facebook Login 通道（token 以 EAA 开头）走 graph.facebook.com，
# 两条通道的 API 形状一致，只是 host 与权限名不同。
GRAPH_HOST_FB = "https://graph.facebook.com"
API_VERSION = "v23.0"

# 容器状态轮询：Meta 建议每分钟查一次、最多 5 分钟
POLL_INTERVAL_SEC = 6
POLL_MAX_SEC = 300


class InstagramError(PublishError):
    pass


@dataclass(frozen=True)
class IgAccount:
    """一个 Instagram 账号的凭据。"""
    key: str            # platforms 配置里的平台名
    ig_user_id: str
    access_token: str

    @property
    def masked_token(self) -> str:
        t = self.access_token or ""
        return f"{t[:6]}…{t[-4:]}" if len(t) > 12 else "（未配置）"

    @property
    def graph_host(self) -> str:
        """按 token 前缀判断该走哪条 Meta 通道。

        - `IGAA...` → Instagram Login（graph.instagram.com）
        - `EAA...`  → Facebook Login（graph.facebook.com）
        两者 API 形状相同，混用 host 会得到
        「Cannot parse access token」这种误导性错误。
        """
        return GRAPH_HOST_FB if self.access_token.startswith("EAA") else GRAPH_HOST

    @property
    def channel(self) -> str:
        return "facebook_login" if self.access_token.startswith("EAA") else "instagram_login"


def load_account(key: str) -> IgAccount | None:
    """从环境变量读取账号凭据。未配置返回 None（由调用方决定是跳过还是报错）。"""
    env_key = re.sub(r"[^A-Za-z0-9]", "_", key).upper()
    token = (os.getenv(f"IG_TOKEN_{env_key}") or "").strip()
    uid = (os.getenv(f"IG_USERID_{env_key}") or "").strip()
    if not token or not uid:
        log.debug("IG 凭据未配置：IG_TOKEN_%s / IG_USERID_%s", env_key, env_key)
        return None
    return IgAccount(key=key, ig_user_id=uid, access_token=token)


# ---------------------------------------------------------------------------
# API 基础调用
# ---------------------------------------------------------------------------

def _request(method: str, path: str, acct: IgAccount, *,
             params: dict | None = None, json_body: dict | None = None,
             timeout: float = 60.0) -> dict:
    """调用 Graph API 并统一处理错误。"""
    url = f"{acct.graph_host}/{API_VERSION}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {acct.access_token}"}
    try:
        with httpx.Client(timeout=timeout) as c:
            if method.upper() == "GET":
                r = c.get(url, params=params or {}, headers=headers)
            else:
                r = c.post(url, params=params or {}, json=json_body or {}, headers=headers)
    except Exception as e:
        raise InstagramError(f"调用 Meta API 网络异常：{e}") from e

    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:300]}

    if r.status_code >= 400:
        err = data.get("error") or {}
        msg = err.get("message") or str(data)[:250]
        code = err.get("code")
        subcode = err.get("error_subcode")
        raise InstagramError(
            f"Meta API 错误 HTTP {r.status_code} code={code} subcode={subcode}：{msg}")
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise InstagramError(
            f"Meta API 返回错误 code={err.get('code')}：{err.get('message')}")
    return data


# ---------------------------------------------------------------------------
# 三步发布流程
# ---------------------------------------------------------------------------

def create_reel_container(acct: IgAccount, video_url: str, caption: str,
                          share_to_feed: bool = True,
                          is_ai_generated: bool = False) -> str:
    """a. 创建 Reels 媒体容器，返回 container id。"""
    body = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption or "",
        "share_to_feed": "true" if share_to_feed else "false",
    }
    if is_ai_generated:
        # Meta 要求 AI 生成内容自我披露（我们搬运的是 AI 动画类素材时有意义）
        body["is_ai_generated"] = "true"

    data = _request("POST", f"{acct.ig_user_id}/media", acct, json_body=body)
    cid = str(data.get("id") or "")
    if not cid:
        raise InstagramError(f"创建容器未返回 id：{data}")
    log.info("[%s] 已创建 Reels 容器 %s", acct.key, cid)
    return cid


def wait_container_ready(acct: IgAccount, container_id: str,
                         max_wait_sec: float = POLL_MAX_SEC) -> str:
    """b. 轮询容器状态直到 FINISHED。返回最终 status_code。"""
    deadline = time.time() + max_wait_sec
    last = ""
    while time.time() < deadline:
        data = _request("GET", container_id, acct,
                        params={"fields": "status_code,status"})
        status = str(data.get("status_code") or "").upper()
        if status != last:
            log.info("[%s] 容器 %s 状态：%s", acct.key, container_id, status or "(空)")
            last = status
        if status == "FINISHED":
            return status
        if status in ("ERROR", "EXPIRED"):
            raise InstagramError(
                f"容器处理失败 status={status}：{data.get('status') or '(无详情)'}")
        time.sleep(POLL_INTERVAL_SEC)

    raise InstagramError(
        f"容器 {container_id} 在 {int(max_wait_sec)} 秒内未就绪（最后状态 {last or '未知'}）")


def publish_container(acct: IgAccount, container_id: str) -> str:
    """c. 正式发布容器，返回 media id。"""
    data = _request("POST", f"{acct.ig_user_id}/media_publish", acct,
                    json_body={"creation_id": container_id})
    media_id = str(data.get("id") or "")
    if not media_id:
        raise InstagramError(f"发布未返回 media id：{data}")
    log.info("[%s] Reels 已发布，media_id=%s", acct.key, media_id)
    return media_id


def fetch_permalink(acct: IgAccount, media_id: str) -> str | None:
    """取已发布作品的永久链接（用于 TG 通知）。失败不抛。"""
    try:
        data = _request("GET", media_id, acct, params={"fields": "permalink"})
        return str(data.get("permalink") or "") or None
    except Exception as e:
        log.debug("[%s] 获取 permalink 失败：%s", acct.key, str(e)[:120])
        return None


def check_publishing_quota(acct: IgAccount) -> int | None:
    """查询 24 小时已用配额（Meta 上限 100）。失败返回 None。"""
    try:
        data = _request("GET", f"{acct.ig_user_id}/content_publishing_limit", acct,
                        params={"fields": "quota_usage,config"})
        items = data.get("data") or []
        if items:
            return int(items[0].get("quota_usage") or 0)
    except Exception as e:
        log.debug("[%s] 查询发布配额失败：%s", acct.key, str(e)[:120])
    return None


# ---------------------------------------------------------------------------
# 统一发布入口（与快手/抖音的 publish() 签名对齐，供 publish_worker 调用）
# ---------------------------------------------------------------------------

def compose_caption(title: str, description: str, tags: list[str]) -> str:
    """拼接 IG Reels 的 caption。

    IG 没有独立标题字段：标题就是 caption 第一行（列表页展示前 ~125 字），
    正文接简介 + hashtags。实测这样比把全部内容揉成一段的点击率高。
    """
    lines: list[str] = []
    t = (title or "").strip()
    d = (description or "").strip()
    if t:
        lines.append(t)
    if d and d != t:
        lines.append("")
        lines.append(d)

    ht: list[str] = []
    for raw in tags or []:
        tag = str(raw).lstrip("#").strip()
        if not tag:
            continue
        tag = re.sub(r"\s+", "", tag)
        if tag and f"#{tag}" not in ht:
            ht.append(f"#{tag}")
    if ht:
        lines.append("")
        lines.append(" ".join(ht))
    return "\n".join(lines).strip()


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
    account_key: str = "",
    share_to_feed: bool = True,
    is_ai_generated: bool = False,
    media_base_url: str = "",
    media_url: str = "",
    **_ignored,
) -> str | None:
    """发布一条 Reel，返回作品永久链接（拿不到返回 None）。

    与快手 publish() 保持同样的调用契约，publish_worker 无需特判。

    媒体来源两种：
    - media_url 非空（分体部署常态）：协调端已生成签名公网 URL，直接用；
    - 否则回退到本地文件 + build_public_url（单机部署，本进程就能对外提供）。

    流程：公网 URL → 创建容器 → 等处理完成 → 发布 → 取 permalink
    """
    from .media_upload import build_public_url

    key = (account_key or "").strip()
    if not key:
        raise InstagramError("缺少 account_key：无法确定用哪个 Instagram 账号凭据")

    acct = load_account(key)
    if acct is None:
        from .base import LoginExpired
        env_key = re.sub(r"[^A-Za-z0-9]", "_", key).upper()
        raise LoginExpired(
            f"未配置 Instagram 凭据：请在 .env 填写 IG_TOKEN_{env_key} 与 IG_USERID_{env_key}")

    used = check_publishing_quota(acct)
    if used is not None and used >= 100:
        raise InstagramError(f"[{key}] 已达 Meta 24 小时发布上限（{used}/100）")

    caption = compose_caption(title, description, tags)
    log.info("[%s] 准备发布 Reel：%s", key, (title or "")[:40])

    public_url = (media_url or "").strip()
    if not public_url:
        try:
            public_url = build_public_url(Path(video), base_url=media_base_url or "")
        except Exception as e:
            raise InstagramError(f"生成公网媒体 URL 失败：{e}") from e

    log.info("[%s] 媒体公网地址：%s", key, public_url.split("?")[0][:120])

    container_id = create_reel_container(
        acct, public_url, caption,
        share_to_feed=share_to_feed, is_ai_generated=is_ai_generated)
    wait_container_ready(acct, container_id)
    media_id = publish_container(acct, container_id)
    return fetch_permalink(acct, media_id)


def login_interactive(state_path: Path | None = None) -> bool:
    """IG 走 API 通道，不需要扫码登录。

    保留此函数是为了满足 PublisherSpec 的接口契约（TG /login 指令会调用它）。
    真正的「登录」是去 .env 配置 long-lived token，这里给出明确指引。
    """
    print("\n>>> Instagram 走 Meta 官方 API，无需扫码登录。")
    print(">>> 请按以下步骤配置：")
    print(">>>   1. 打开 https://developers.facebook.com/apps 创建应用")
    print(">>>   2. 添加 Instagram 产品，用目标账号授权")
    print(">>>   3. 换取 long-lived token（约 60 天有效）")
    print(">>>   4. 在 .env 写入：")
    print(">>>        IG_TOKEN_<账号KEY>=<token>")
    print(">>>        IG_USERID_<账号KEY>=<ig_user_id>")
    print(">>>   5. 配好后跑 python -m bot.main --ig-check 验证凭据\n")
    return False

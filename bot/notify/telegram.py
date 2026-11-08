"""Telegram Bot API 通知。通知失败只记日志，绝不阻断主流程。"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

import httpx

from ..config import Settings

log = logging.getLogger(__name__)


def _post(s: Settings, method: str, *, json_payload: dict | None = None,
          form: dict | None = None, files: dict | None = None,
          extra_json: dict | None = None) -> dict | None:
    """发一个 Telegram Bot API 请求。通知失败只记日志，不阻断主流程。

    extra_json: 追加到 JSON payload 的额外字段（如 reply_markup）。
    返回 API 响应 dict（调用方需要 result 时用），失败返回 None。
    """
    if not (s.telegram_bot_token and s.telegram_chat_id):
        log.warning("Telegram 未配置（TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID），跳过通知")
        return None
    url = f"{s.telegram_api_base}/bot{s.telegram_bot_token}/{method}"
    client_kw: dict = {"timeout": 30}
    if s.telegram_proxy:
        client_kw["proxy"] = s.telegram_proxy
    try:
        with httpx.Client(**client_kw) as client:
            if files is not None:
                data = {"chat_id": s.telegram_chat_id, **(form or {})}
                if extra_json:
                    for k, v in extra_json.items():
                        data[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                resp = client.post(url, data=data, files=files)
            else:
                payload = {"chat_id": s.telegram_chat_id, **(json_payload or {})}
                if extra_json:
                    payload.update(extra_json)
                resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            log.error("Telegram 返回错误：%s", resp.text[:200])
            return None
        return data
    except Exception as e:
        log.error("Telegram 通知发送失败（%s）：%s", method, e)
        return None


def send_text(s: Settings, text: str, reply_markup: dict | None = None) -> None:
    _post(s, "sendMessage",
          json_payload={"text": text, "disable_web_page_preview": True},
          extra_json={"reply_markup": reply_markup} if reply_markup else None)


def set_commands(s: Settings, commands: list[tuple[str, str]]) -> bool:
    """注册 bot 命令菜单（setMyCommands）：TG 输入 / 直接点选。

    幂等：每次监听器启动都调一次，改菜单只改代码。
    """
    if not s.telegram_bot_token:
        return False
    url = f"{s.telegram_api_base}/bot{s.telegram_bot_token}/setMyCommands"
    client_kw: dict = {"timeout": 30}
    if s.telegram_proxy:
        client_kw["proxy"] = s.telegram_proxy
    try:
        with httpx.Client(**client_kw) as client:
            resp = client.post(url, json={
                "commands": [{"command": c, "description": d} for c, d in commands]})
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            log.error("setMyCommands 返回错误：%s", resp.text[:200])
            return False
        return True
    except Exception as e:
        log.error("setMyCommands 失败：%s", e)
        return False


def send_photo(s: Settings, photo: Path | None, caption: str,
               reply_markup: dict | None = None) -> None:
    if photo and Path(photo).exists():
        with Path(photo).open("rb") as f:
            _post(s, "sendPhoto", form={"caption": caption},
                  files={"photo": (Path(photo).name, f, "image/jpeg")},
                  extra_json={"reply_markup": reply_markup} if reply_markup else None)
    else:
        send_text(s, caption, reply_markup=reply_markup)


def notify_published(s: Settings, task: dict, jobs: list[dict] | None = None) -> None:
    """jobs: publish_jobs 行（带 display_name 字段）；无 jobs 时回退单平台旧格式。"""
    title = task.get("title", "")
    source = task.get("permalink") or task.get("shortcode", "")
    if jobs:
        lines = [f"✅ 发布完成 [{task.get('shortcode', '')}]", f"标题：{title}"]
        for j in jobs:
            name = j.get("display_name") or j.get("platform", "?")
            if j["state"] == "PUBLISHED":
                url = j.get("url") or "（链接未获取到，见创作者中心）"
                lines.append(f"• {name} ✅ {url}")
            elif j["state"] == "PENDING":
                lines.append(f"• {name} ⏳ 待发布（排队中）")
            elif j["state"] == "SKIPPED":
                lines.append(f"• {name} ⏭️ 已跳过：{(j.get('error') or '')[:80]}")
            else:
                lines.append(f"• {name} ❌ 失败：{(j.get('error') or '')[:80]}")
        lines.append(f"来源：{source}")
    else:
        lines = [
            "✅ 快手发布成功",
            f"标题：{title}",
            f"分区：{task.get('category') or '（未选）'}",
            f"标签：{task.get('tags') or '（无）'}",
            f"快手链接：{task.get('ks_url') or '（未获取到，见创作者中心-内容管理）'}",
            f"来源：{source}",
        ]
    cover = Path(task["cover_path"]) if task.get("cover_path") else None
    send_photo(s, cover, "\n".join(lines))


def notify_failed(s: Settings, task: dict, error: Exception) -> None:
    send_text(s, (
        f"❌ 任务失败 [{task.get('shortcode')}] 在 {task.get('state')} 阶段\n"
        f"{str(error)[:400]}\n"
        f"处理建议：修复后运行 python -m bot.main --retry-failed 重跑"
    ))


def notify_info(s: Settings, text: str) -> None:
    send_text(s, f"ℹ️ {text}")


def discover_chat_ids(s: Settings) -> list[str]:
    """从 getUpdates 里找出给 bot 发过消息的 chat id（--setup 向导用）。"""
    if not s.telegram_bot_token:
        return []
    url = f"{s.telegram_api_base}/bot{s.telegram_bot_token}/getUpdates"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url)
        data = resp.json()
    except Exception as e:
        log.debug("getUpdates 失败：%s", e)
        return []
    ids: list[str] = []
    for upd in data.get("result", []):
        chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat", {})
        cid = str(chat.get("id", ""))
        if cid and cid not in ids:
            ids.append(cid)
    return ids


# ---------- 回调交互（/login 菜单按钮） ----------

def answer_callback(s: Settings, callback_query_id: str, text: str = "",
                    show_alert: bool = False) -> None:
    """回应 callback_query（否则 TG 客户端按钮会一直转圈）。"""
    _post(s, "answerCallbackQuery",
          json_payload={"callback_query_id": callback_query_id,
                        "text": text[:200], "show_alert": show_alert})


# ---------- 文件下载（本地文件投链） ----------

# 官方 Bot API 的单文件下载上限是 20MB（getFile 限制）。
# 自建 Bot API server 且开 --local 模式时上限提高到 2000MB。
TG_MAX_DOWNLOAD_MB_OFFICIAL = 20
TG_MAX_DOWNLOAD_MB_LOCAL = 2000
# 兼容旧引用（测试/调用方按这个名字取值）
TG_MAX_DOWNLOAD_MB = TG_MAX_DOWNLOAD_MB_OFFICIAL


def is_local_bot_api(s: Settings) -> bool:
    """是否连自建 Bot API server（本机/内网地址，非 api.telegram.org）。"""
    base = (getattr(s, "telegram_api_base", "") or "").lower()
    return bool(base) and "api.telegram.org" not in base


def max_download_mb(s: Settings) -> int:
    """当前 Bot API 通道允许的单文件大小上限（MB）。"""
    return TG_MAX_DOWNLOAD_MB_LOCAL if is_local_bot_api(s) else TG_MAX_DOWNLOAD_MB_OFFICIAL


def get_file_result(s: Settings, file_id: str) -> dict | None:
    """调 getFile，返回完整 result（含 file_path / file_size）。"""
    resp = _post(s, "getFile", json_payload={"file_id": file_id})
    if not resp:
        return None
    result = resp.get("result") or {}
    return result or None


def get_file_path(s: Settings, file_id: str) -> str | None:
    """调 getFile 拿文件在 TG 服务器上的路径。"""
    result = get_file_result(s, file_id)
    return (result or {}).get("file_path") or None


def download_file(s: Settings, file_id: str, dest: Path,
                  max_mb: int | None = None) -> Path | None:
    """把 Telegram 上的文件取到本地 dest。

    两条路径（自动选择）：
    1. **自建 Bot API（--local）**：getFile 返回的 file_path 是服务器绝对路径，
       直接 hardlink 或复制即可，**不走 HTTP、不限大小、不重复传输**；
    2. **官方 api.telegram.org**：走 /file/bot<token>/<path> 下载，上限 20MB。

    返回 dest（成功）或 None（失败，已记日志）。
    """
    result = get_file_result(s, file_id)
    if not result:
        log.error("getFile 失败，无法获取文件信息（file_id=%s）", file_id[:40])
        return None
    file_path = result.get("file_path") or ""
    if not file_path:
        log.error("getFile 未返回 file_path（file_id=%s）", file_id[:40])
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)

    # ---- 路径 1：自建 local server，file_path 就是本机绝对路径 ----
    if is_local_bot_api(s) and file_path.startswith("/"):
        src = Path(file_path)
        if src.is_file():
            try:
                # 优先硬链接（同分区零拷贝）；失败退回复制
                if dest.exists():
                    dest.unlink()
                try:
                    os.link(src, dest)
                    log.info("TG 本地文件已硬链接：%s（%.1f MB）",
                             dest.name, dest.stat().st_size / 1048576)
                except OSError:
                    shutil.copyfile(src, dest)
                    log.info("TG 本地文件已复制：%s（%.1f MB）",
                             dest.name, dest.stat().st_size / 1048576)
                return dest
            except Exception as e:
                log.warning("本地文件取用失败，回退 HTTP 下载：%s", str(e)[:120])
        else:
            log.debug("file_path 不是可读的本机文件（%s），回退 HTTP 下载", file_path[:80])

    # ---- 路径 2：官方 API，走 HTTP 下载 ----
    limit_mb = max_mb if max_mb is not None else max_download_mb(s)
    size = int(result.get("file_size") or 0)
    if size and size / 1048576 > limit_mb:
        log.error("文件 %.1f MB 超过当前通道上限 %d MB",
                  size / 1048576, limit_mb)
        return None

    url = f"{s.telegram_api_base}/file/bot{s.telegram_bot_token}/{file_path}"
    client_kw: dict = {"timeout": 600}
    if s.telegram_proxy:
        client_kw["proxy"] = s.telegram_proxy

    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with httpx.Client(**client_kw, follow_redirects=True) as client:
            with client.stream("GET", url) as r:
                if r.status_code != 200:
                    log.error("下载 TG 文件失败：HTTP %d", r.status_code)
                    return None
                size = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        size += len(chunk)
        tmp.replace(dest)
        log.info("TG 文件已下载：%s（%.1f MB）", dest.name, dest.stat().st_size / 1048576)
        return dest
    except Exception as e:
        log.error("下载 TG 文件异常：%s", str(e)[:200])
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None

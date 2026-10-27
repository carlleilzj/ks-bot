"""Telegram Bot API 通知。通知失败只记日志，绝不阻断主流程。"""

from __future__ import annotations

import json
import logging
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

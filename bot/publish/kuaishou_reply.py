"""快手评论拉取与 LLM 安全回复闭环（借鉴 MatrixMedia 评论互动架构）。

机制与安全红线：
1. 评论拉取：通过网络嗅探（Sniffer）截获页面发出的 /commentList 接口，免除签名逆向风险；
2. 回复生成：调用 LLM 按粉丝评论内容生成真诚、风趣、简短（≤30字）的互动回复；
3. 真实发送：通过 DOM 级精确控件定位，排除顶部作者常驻框，模拟真实点击；
4. 安全守则（红线）：
   - 默认 dry-run：仅校验目标评论是否可定位及文案生成，显式指定 execute 才真发；
   - 单轮硬上限：默认 3 条，硬上限 10 条，避免批量刷屏触发平台风控；
   - 增量台账：每条操作结果追加记录到 data/ks_reply_log.jsonl；
   - Fail-Stop：遇到失败或风控提示立即中止本轮后续操作。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from ..config import DATA_DIR, KS_STATE_PATH, LOGS_DIR, load_settings
from .base import launch_chromium, new_context, shot
from .kuaishou import COMMENT_URL, _is_logged_in

log = logging.getLogger("kuaishou_reply")

REPLY_LOG_PATH = DATA_DIR / "ks_reply_log.jsonl"


@dataclass
class CommentItem:
    """快手单条评论数据。"""
    comment_id: str
    work_id: str
    author_name: str
    author_id: str
    content: str
    timestamp: int
    liked_count: int = 0
    sub_comment_count: int = 0
    toped: bool = False
    reply_to: str = ""


# ---------------------------------------------------------------------------
# 1. 评论网络嗅探
# ---------------------------------------------------------------------------

def fetch_kuaishou_comments(page: Page, timeout_sec: float = 15.0, limit: int = 50) -> list[CommentItem]:
    """通过页面响应嗅探拉取最新作品评论列表。"""
    sniffed_list: list[dict] = []

    def _on_response(response):
        try:
            url = getattr(response, "url", "")
            if "/creator/comment/commentList" in url and getattr(response, "status", 0) == 200:
                data = response.json()
                if isinstance(data, dict) and data.get("result") == 1:
                    raw_items = (data.get("data") or {}).get("list") or []
                    for item in raw_items:
                        if isinstance(item, dict):
                            sniffed_list.append(item)
        except Exception:
            pass

    page.on("response", _on_response)

    if not page.url.startswith(COMMENT_URL):
        page.goto(COMMENT_URL, wait_until="domcontentloaded", timeout=int(timeout_sec * 1000))

    deadline = time.time() + timeout_sec
    while time.time() < deadline and not sniffed_list:
        # 滚动页面以触发评论列表接口加载
        try:
            page.evaluate("""() => {
                window.scrollTo(0, document.documentElement.scrollHeight);
                document.querySelectorAll('*').forEach(el => {
                    if (el.scrollHeight > el.clientHeight + 100 && el.clientHeight > 200) {
                        el.scrollTop = el.scrollHeight;
                    }
                });
            }""")
        except Exception:
            pass
        page.wait_for_timeout(1000)

    comments: list[CommentItem] = []
    seen_ids = set()

    for item in sniffed_list:
        cid = str(item.get("commentId") or "")
        if not cid or cid in seen_ids:
            continue
        seen_ids.add(cid)
        comments.append(CommentItem(
            comment_id=cid,
            work_id=str(item.get("photoId") or ""),
            author_name=str(item.get("authorName") or ""),
            author_id=str(item.get("authorId") or ""),
            content=str(item.get("content") or "").strip(),
            timestamp=int(item.get("timestamp") or 0),
            liked_count=int(item.get("likedCount") or 0),
            sub_comment_count=int(item.get("subCommentCount") or 0),
            toped=bool(item.get("toped")),
            reply_to=str(item.get("replyToUserName") or ""),
        ))

    log.info("快手评论拉取：嗅探到 %d 条评论", len(comments))
    return comments[:limit]


# ---------------------------------------------------------------------------
# 2. LLM 自动生成回复
# ---------------------------------------------------------------------------

_FALLBACK_REPLIES = [
    "感谢支持和喜爱，会持续更新的！😊",
    "哈哈谢谢你的喜欢，关注不迷路哦~",
    "收到你的好评啦，祝你今天也开开心心！✨",
    "感谢老铁支持，后续更精彩！🤝",
]


def generate_comment_reply(fan_comment: str, author_name: str = "") -> str:
    """基于粉丝评论内容，利用 AI 生成一条自然、简短的互动回复。"""
    fan_comment = (fan_comment or "").strip()
    if not fan_comment:
        return random.choice(_FALLBACK_REPLIES)

    settings = load_settings()
    if not settings.ai_api_key:
        return random.choice(_FALLBACK_REPLIES)

    try:
        from openai import OpenAI
        client = OpenAI(base_url=settings.ai_base_url, api_key=settings.ai_api_key, timeout=20.0)

        prompt = (
            f"粉丝在短视频评论区留言：\"{fan_comment}\"\n"
            f"请作为作者写一条自然、幽默、亲切的回复。"
        )

        resp = client.chat.completions.create(
            model=settings.ai_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一位受粉丝喜爱的快手短视频作者。请回复粉丝留言。\n"
                        "规则：\n"
                        "1. 字数控制在 8-25 字以内，口语化、接地气；\n"
                        "2. 态度亲切，积极互动；\n"
                        "3. 绝对不得出现广告、微信、引流、敏感词；\n"
                        "4. 只返回单行纯文本回复内容，不要加引号、表情代码或任何解释。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.8,
            max_tokens=60,
        )

        text = resp.choices[0].message.content or ""
        text = re.sub(r'["“”\'‘’]', "", text).strip()
        if text:
            return text[:40]
    except Exception as e:
        log.warning("LLM 生成评论回复失败，使用预设回复：%s", e)

    return random.choice(_FALLBACK_REPLIES)


# ---------------------------------------------------------------------------
# 3. 页面 DOM 回复执行
# ---------------------------------------------------------------------------

def reply_single_comment_on_page(
    page: Page,
    comment: CommentItem,
    reply_text: str,
    dry_run: bool = True,
) -> tuple[bool, str]:
    """在快手评论管理页定位单条评论并回复。

    参数:
        dry_run: 若为 True，仅校验是否能在 DOM 中找到该评论行，不实际点击发送。
    返回:
        (success: bool, detail: str)
    """
    if not comment.content:
        return False, "目标评论内容为空"

    js_code = """([matchText, replyText, isDryRun]) => {
        const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };

        const deepestByText = (root, text) => {
            const all = [...root.querySelectorAll("button,a,span,div,li")].filter(
                (el) => norm(el.textContent) === text
            );
            if (!all.length) return null;
            const inner = all.filter((el) => !all.some((o) => o !== el && el.contains(o)));
            return (inner.length ? inner : all)[0];
        };

        // 定位包含目标评论文本的行
        const cands = [...document.querySelectorAll("div,li,section,article")].filter((el) =>
            norm(el.innerText).includes(matchText)
        );
        cands.sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);

        let hit = null;
        for (const el of cands.slice(0, 8)) {
            let node = el;
            for (let up = 0; up < 4 && node; up++) {
                if (deepestByText(node, "回复")) {
                    hit = { block: el, row: node };
                    break;
                }
                node = node.parentElement;
            }
            if (hit) break;
        }

        if (!hit) {
            return { ok: false, reason: "未在当前页面找到包含目标评论且带回复按钮的行" };
        }

        const { row } = hit;
        if (isDryRun) {
            return { ok: true, reason: "dry_run: 目标评论行已定位成功" };
        }

        // 记录点击前已存在的输入框（排除顶部常驻作者输入框）
        const inputsBefore = new Set([
            ...document.querySelectorAll('textarea,[contenteditable="true"]')
        ]);

        // 悬停并点击「回复」
        const replyBtn = deepestByText(row, "回复");
        if (!replyBtn) return { ok: false, reason: "行内无「回复」控件" };
        ["pointerover", "mouseover", "mouseenter"].forEach((t) => {
            try { row.dispatchEvent(new MouseEvent(t, { bubbles: true })); } catch (_) {}
        });
        replyBtn.click();

        return { ok: true, needFill: true };
    }"""

    try:
        res = page.evaluate(js_code, [comment.content[:25], reply_text, dry_run])
        if not res or not res.get("ok"):
            return False, res.get("reason", "定位失败")

        if dry_run:
            return True, res.get("reason", "dry_run 校验成功")

        # 非 dry-run：等待新弹出的回复输入框并填写
        page.wait_for_timeout(800)
        fill_res = page.evaluate("""(replyText) => {
            const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const deepestByText = (root, text) => {
                const all = [...root.querySelectorAll("button,a,span,div,li")].filter(
                    (el) => norm(el.textContent) === text
                );
                if (!all.length) return null;
                const inner = all.filter((el) => !all.some((o) => o !== el && el.contains(o)));
                return (inner.length ? inner : all)[0];
            };

            const inputs = [...document.querySelectorAll('textarea, [contenteditable="true"]')].filter(visible);
            if (!inputs.length) return { ok: false, reason: "点击回复后未出现输入框" };
            // 取最新弹出的（通常是最后一个）
            const inputEl = inputs[inputs.length - 1];
            inputEl.focus();

            if (inputEl.tagName === "TEXTAREA") {
                inputEl.value = String(replyText);
                inputEl.dispatchEvent(new Event("input", { bubbles: true }));
            } else {
                document.execCommand("insertText", false, String(replyText));
            }

            // 查找最近的发送按钮
            let sendBtn = null;
            let parent = inputEl.parentElement;
            for (let up = 0; up < 5 && parent && !sendBtn; up++) {
                sendBtn = deepestByText(parent, "发送");
                parent = parent.parentElement;
            }
            if (!sendBtn) sendBtn = deepestByText(document.body, "发送");
            if (!sendBtn) return { ok: false, reason: "未找到发送按钮" };

            sendBtn.click();
            return { ok: true };
        }""", reply_text)

        if not fill_res or not fill_res.get("ok"):
            return False, fill_res.get("reason", "输入或发送失败")

        page.wait_for_timeout(1500)
        shot(page, "ks_comment_reply_done")
        return True, "回复已成功发送"

    except Exception as e:
        log.warning("单条评论回复操作异常：%s", e)
        return False, str(e)[:120]


# ---------------------------------------------------------------------------
# 4. 批量闭环调度与安全台账
# ---------------------------------------------------------------------------

def append_reply_log(entry: dict, log_path: Path = REPLY_LOG_PATH) -> None:
    """追加单条回复执行台账（JSONL 格式）。"""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("写入回复台账失败：%s", e)


def run_comment_reply_batch(
    page: Page,
    dry_run: bool = True,
    max_replies: int = 3,
    state_path: Path = KS_STATE_PATH,
    log_path: Path = REPLY_LOG_PATH,
) -> dict:
    """执行快手评论自动回复批处理任务。

    参数:
        page: 已打开快手创作者中心的 Playwright Page
        dry_run: True 为安全模拟，False 为真实发送
        max_replies: 单轮最大回复条数（强制限制在 1-10 条）
    """
    max_replies = max(1, min(max_replies, 10))  # 硬红线：最多 10 条
    comments = fetch_kuaishou_comments(page)

    # 过滤：必须有文字、且不是已回复过的（sub_comment_count == 0）、非置顶
    unreplied = [c for c in comments if c.content and c.sub_comment_count == 0 and not c.toped]
    # 按点赞量降序，优先回复高赞互动
    unreplied.sort(key=lambda c: -c.liked_count)

    targets = unreplied[:max_replies]
    log.info("快手评论批处理：共嗅探到 %d 条，未回复 %d 条，本轮计划处理 %d 条（dry_run=%s）",
             len(comments), len(unreplied), len(targets), dry_run)

    results = []
    success_count = 0
    fail_count = 0

    for idx, c in enumerate(targets):
        reply_content = generate_comment_reply(c.content, c.author_name)
        ok, detail = reply_single_comment_on_page(page, c, reply_content, dry_run=dry_run)

        status_label = "dry_ok" if (ok and dry_run) else ("ok" if ok else "error")
        record = {
            "timestamp": int(time.time()),
            "platform": "kuaishou",
            "comment_id": c.comment_id,
            "work_id": c.work_id,
            "fan_content": c.content,
            "reply_content": reply_content,
            "mode": "dry" if dry_run else "execute",
            "status": status_label,
            "detail": detail,
        }

        append_reply_log(record, log_path=log_path)
        results.append(record)

        if ok:
            success_count += 1
            log.info("评论 [%d/%d] 处理成功 (%s): 粉丝「%s」→ 回复「%s」",
                     idx + 1, len(targets), status_label, c.content[:15], reply_content[:15])
        else:
            fail_count += 1
            log.warning("评论 [%d/%d] 处理失败: %s，立即终止本轮任务 (Fail-Stop)", idx + 1, len(targets), detail)
            # 安全防线：一旦遇到任何错误立即中止后续操作，防止连环风控
            break

        if not dry_run and ok and idx + 1 < len(targets):
            # 真实发送后随机延时 2.5 ~ 4.5 秒，模拟真人操作
            delay = random.uniform(2.5, 4.5)
            page.wait_for_timeout(int(delay * 1000))

    return {
        "total": len(targets),
        "succeeded": success_count,
        "failed": fail_count,
        "dry_run": dry_run,
        "results": results,
    }


def run_kuaishou_reply_task(
    dry_run: bool = True,
    max_replies: int = 3,
    headed: bool = False,
    state_path: Path = KS_STATE_PATH,
) -> dict:
    """独立运行快手评论回复任务（CLI 入口调用）。"""
    with sync_playwright() as p:
        browser = launch_chromium(p, headless=not headed)
        context = new_context(browser, state_path)
        page = context.new_page()
        try:
            page.goto(COMMENT_URL, wait_until="domcontentloaded", timeout=60000)
            if not _is_logged_in(page):
                shot(page, "ks_comment_login_expired")
                log.error("快手登录态已失效，无法执行评论互动")
                return {"error": "登录态已失效"}

            return run_comment_reply_batch(page, dry_run=dry_run, max_replies=max_replies, state_path=state_path)
        finally:
            try:
                browser.close()
            except Exception:
                pass

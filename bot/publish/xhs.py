"""小红书创作者中心（creator.xiaohongshu.com）Playwright 自动化：扫码登录 + 上传发布。

注意：
- 标题是独立输入框，上限 20 字（文案层已按此约束生成，硬限制）
- 无分区概念，话题标签（#xxx）写进正文
- 选择器集中在 SELECTORS，页面改版后只需调整这里
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from ..config import DATA_DIR
from .base import (
    UA,
    LoginExpired,
    PublishError,
    dismiss_dialogs,
    fill_editor,
    launch_chromium,
    launch_persistent_chromium,
    new_context,
    rand_sleep,
    settle,
    shot,
    wait_upload_done,
)

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "xhs_state.json"

PUBLISH_URL = "https://creator.xiaohongshu.com/publish/paste"
LOGIN_URL = "https://creator.xiaohongshu.com/"   # 直接访问 /login 会被 SPA 渲染成 404，须从首页跳转
HOME_URL = "https://creator.xiaohongshu.com/new/home"  # 发布入口须从首页内部点进去，直接访问 /publish/* 也是 404

UPLOAD_TIMEOUT = 15 * 60

SELECTORS = {
    "file_input": "input[type='file']",
    "upload_tab_text": "上传视频",   # 落在粘贴页时需要先切到上传视频 tab
    "title_input": [
        "#title-textarea",
        'input[placeholder*="标题"]',
        'input[placeholder*="填写标题"]',
        'input.d-text[placeholder]',
        'textarea[placeholder*="标题"]',
    ],
    "desc_editor": [
        '#post-textarea',
        "div.tiptap.ProseMirror[contenteditable]",
        'div[contenteditable="true"]',
        'textarea[placeholder*="描述"]',
        "textarea",
    ],
    "publish_btn_text": "发布",
    "upload_done_texts": ["重新上传", "上传完成", "上传成功"],
    "upload_fail_texts": ["上传失败", "上传出错"],
    "success_texts": ["发布成功", "已提交"],
}


class XhsError(PublishError):
    pass


def _is_logged_in(page: Page) -> bool:
    try:
        if "login" in page.url:
            return False
        return page.locator(SELECTORS["file_input"]).count() > 0
    except Exception:
        return False


def _has_login_cookies(context) -> bool:
    """按 cookie 判定登录态。小红书创作者中心的会话 cookie 是
    galaxy_creator_session_id / customer-sso-sid（不是 web_session）。"""
    try:
        cookies = context.cookies(["https://creator.xiaohongshu.com", "https://www.xiaohongshu.com"])
        names = {c["name"] for c in cookies}
        return bool({"galaxy_creator_session_id", "galaxy.creator.beaker.session.id",
                     "customer-sso-sid", "web_session"} & names)
    except Exception:
        return False


# ---------- 登录 ----------

def login_interactive(state_path: Path = STATE_PATH) -> bool:
    """打开有头浏览器扫码登录，保存登录态。持久化 profile（中途崩溃 cookie 不丢）。"""
    from playwright.sync_api import sync_playwright

    from ..config import DATA_DIR as _DATA
    profile = _DATA / "xhs_profile"
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = launch_persistent_chromium(
            p, profile, headless=False, slow_mo=150,
            user_agent=UA, viewport={"width": 1440, "height": 900},
            locale="zh-CN", timezone_id="Asia/Shanghai",
        )
        context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = context.pages[0] if context.pages else context.new_page()
        # 直接访问 /login 会被 SPA 渲染成 404，必须从首页跳转
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)
        print("\n>>> 浏览器已打开小红书创作者中心，请用小红书 App 扫码登录（5 分钟内有效）...\n")
        deadline = time.time() + 300
        while time.time() < deadline:
            url = page.url
            logged_in = ("/login" not in url and "passport" not in url
                         and url.rstrip("/") != "https://creator.xiaohongshu.com")
            if (logged_in and _has_login_cookies(context)) or _has_login_cookies(context):
                time.sleep(2)
                state_path.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(state_path))
                print(f">>> 登录成功！登录态已保存到 {state_path}")
                context.close()
                return True
            time.sleep(2)
        print(">>> 等待登录超时，未保存登录态")
        context.close()
        return False


# ---------- 发布 ----------

def _goto_upload_tab(page: Page) -> None:
    """发布页（/publish/paste）需切到「上传视频」tab 才有视频上传入口。"""
    try:
        tab = page.get_by_text(SELECTORS["upload_tab_text"], exact=False).first
        if tab.count() and tab.is_visible():
            tab.click()
            rand_sleep(0.5, 1.0)
            log.info("已切换到「上传视频」tab")
    except Exception as e:
        log.debug("切换上传视频 tab 失败：%s", e)


def publish(
    video: Path,
    title: str,
    description: str,
    tags: list[str],
    category: str | None,       # 小红书无分区，忽略
    cover: Path | None = None,  # 小红书视频笔记封面来自首帧，暂不自定义
    headless: bool = True,
    state_path: Path = STATE_PATH,
) -> str | None:
    """上传并发布一条视频，成功返回笔记链接（获取不到时 None）。"""
    if not Path(state_path).exists():
        raise LoginExpired("未找到小红书登录态，请先运行: python -m bot.main --login xhs")

    tag_str = " ".join(f"#{t}" for t in tags if t)
    desc_full = "\n".join(x for x in (description, tag_str) if x)

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=headless)
        context = new_context(browser, state_path)
        page = context.new_page()
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            settle(page)
            if not _has_login_cookies(context):
                shot(page, "xhs_login_expired")
                raise LoginExpired("小红书登录态已失效，请运行 python -m bot.main --login xhs 重新扫码")
            dismiss_dialogs(page)

            # 发布入口必须从首页内部点进去（直接访问 /publish/* 会被 SPA 渲染成 404）
            entry = None
            for text in ("发布笔记", "发布视频", "发布"):
                btn = page.get_by_text(text, exact=False).first
                if btn.count() and btn.is_visible():
                    entry = text
                    btn.click()
                    rand_sleep()
                    break
            if not entry:
                shot(page, "xhs_publish_entry_fail")
                raise XhsError("未找到发布入口，截图见 logs/（页面可能已改版）")
            log.info("已点击发布入口（%s）", entry)
            # 弹层里可能有「上传视频/上传图文」两个选项
            _goto_upload_tab(page)

            # 1. 上传视频（等上传 input 渲染出来）
            file_input = page.locator(SELECTORS["file_input"]).first
            file_input.wait_for(state="attached", timeout=15000)
            file_input.set_input_files(str(video))
            log.info("已提交视频上传：%s", video.name)

            # 2. 等待上传完成
            wait_upload_done(page, SELECTORS["upload_done_texts"], SELECTORS["upload_fail_texts"],
                             shot_prefix="xhs", timeout=UPLOAD_TIMEOUT)
            page.wait_for_timeout(5000)
            dismiss_dialogs(page)
            rand_sleep()

            # 3. 标题（独立输入框，≤20 字）
            _fill_title(page, title[:20])

            # 4. 正文 + 话题标签
            fill_editor(page, desc_full, shot_prefix="xhs_desc",
                        candidates=[page.locator(c) for c in SELECTORS["desc_editor"]])

            # 5. 点发布
            _click_publish(page)

            xhs_url = _fetch_xhs_url(context)
            shot(page, "xhs_publish_done")
            log.info("小红书发布成功：%s", xhs_url or "（笔记链接获取失败，见创作者中心-内容管理）")
            return xhs_url
        finally:
            context.close()
            browser.close()


def _fill_title(page: Page, title: str) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        for sel in SELECTORS["title_input"]:
            loc = page.locator(sel).first
            try:
                if loc.count() and loc.is_visible():
                    loc.click()
                    page.keyboard.press("Control+a")
                    page.keyboard.press("Backspace")
                    page.keyboard.type(title, delay=40)
                    log.info("已填写小红书标题（%d 字）", len(title))
                    return
            except Exception:
                continue
        time.sleep(1)
    shot(page, "xhs_title_fail")
    raise XhsError("未找到小红书标题输入框，截图见 logs/（页面可能已改版）")


def _find_publish_button_px(page: Page) -> tuple[int, int] | None:
    """底部红色「发布」按钮在封闭 Shadow DOM 里，DOM 查询永远找不到；
    改用截图 + 像素分析定位（底部区域的红色大按钮），返回视口坐标。"""
    try:
        import io

        from PIL import Image
        png = page.screenshot()
        img = Image.open(io.BytesIO(png)).convert("RGB")
        w, h = img.size
        xs, ys = [], []
        for y in range(int(h * 0.80), h):
            for x in range(int(w * 0.40), w):
                r, g, b = img.getpixel((x, y))
                if r > 170 and g < 120 and b < 120:
                    xs.append(x)
                    ys.append(y)
        if len(xs) < 50:  # 像素太少视为噪声
            return None
        xs.sort()
        ys.sort()
        return xs[len(xs) // 2], ys[len(ys) // 2]  # 中位数，抗离群点
    except Exception as e:
        log.debug("像素定位发布按钮失败：%s", e)
        return None


def _js_click_publish(page: Page) -> bool:
    """穿透普通 DOM + open Shadow DOM 找底部「发布」并 click()。"""
    return bool(page.evaluate("""() => {
        const isPublish = (el) => {
            const t = (el.innerText || el.textContent || "").trim();
            return t === "发布" || t === "发布笔记";
        };
        const walk = (root) => {
            const nodes = root.querySelectorAll("button, div, span, a");
            for (const el of nodes) {
                if (!isPublish(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 40 || r.height < 20 || r.top < window.innerHeight * 0.6) continue;
                el.click();
                return true;
            }
            for (const el of root.querySelectorAll("*")) {
                if (el.shadowRoot && walk(el.shadowRoot)) return true;
            }
            return false;
        };
        return walk(document);
    }"""))


def _click_publish(page: Page) -> None:
    # 先关掉话题联想残留的「无结果」浮层等弹窗
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(400)
    dismiss_dialogs(page, extra_texts=("我知道了", "知道了", "确定"))

    # 注意：edith.xiaohongshu.com/web_api/sns/v2/note 返回 200 只是「保存草稿」，
    # 账号被风控（如「违反社区规范禁止发笔记」）时也会返回 200，不能作为发布成功依据。
    # 成功判定以「页面跳转离开发布页 + 无违规弹窗」为准。

    clicked = False
    # 1. DOM 角色按钮（若发布按钮不在封闭 Shadow DOM 里）
    for name in (SELECTORS["publish_btn_text"], "发布笔记"):
        try:
            btn = page.get_by_role("button", name=name, exact=True).last
            if btn.count() and btn.is_visible():
                btn.click()
                log.info("点击发布按钮（DOM role=%s）", name)
                clicked = True
                break
        except Exception:
            continue

    # 2. JS 穿透 open shadow root
    if not clicked:
        try:
            if _js_click_publish(page):
                log.info("点击发布按钮（JS / shadow）")
                clicked = True
        except Exception as e:
            log.debug("JS 点击发布失败：%s", e)

    # 3. 像素兜底
    if not clicked:
        pos = _find_publish_button_px(page)
        if pos:
            log.info("像素定位到发布按钮 @(%d,%d)，物理点击", *pos)
            page.mouse.click(*pos)
            clicked = True
        else:
            shot(page, "xhs_publish_btn_fail")
            raise XhsError("未定位到发布按钮（底部无红色按钮），截图见 logs/")

    deadline = time.time() + 60
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        # 风控弹窗：账号被限制发布时点发布只会保存草稿
        for text in ("违反社区规范", "禁止发笔记", "暂时无法发布", "发布失败"):
            if page.get_by_text(text, exact=False).count():
                shot(page, "xhs_publish_blocked")
                raise XhsError(f"小红书账号发布受限（页面提示「{text}」），可能是风控/违规限制，"
                               f"请人工检查创作者中心")
        for text in SELECTORS["success_texts"]:
            if page.get_by_text(text, exact=False).count():
                return
        # 发布成功的可靠标志：URL 跳转离开发布页
        if "/publish/success" in page.url or "note-manager" in page.url or "manage" in page.url:
            return
        # 仍在发布页（URL 未变）说明只是保存草稿，继续等
        # 可能弹出二次确认
        for text in ("确认发布", "确定发布", "确认", "确定"):
            try:
                confirm = page.get_by_role("button", name=text, exact=False).first
                if confirm.count() and confirm.is_visible():
                    confirm.click()
                    log.info("点击确认发布（%s）", text)
                    break
            except Exception:
                continue
    shot(page, "xhs_publish_result_unknown")
    raise XhsError("点击发布后 60 秒内未检测到成功标志，请查看 logs/ 截图确认")


def _fetch_xhs_url(context) -> str | None:
    """发布成功后到内容管理页抓最新笔记链接（尽力而为）。"""
    try:
        page = context.new_page()
        page.goto("https://creator.xiaohongshu.com/new/note-manager",
                  wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(4000)
        link = page.locator("a[href*='/explore/'], a[href*='/discovery/item/']").first
        if link.count():
            href = link.get_attribute("href") or ""
            if href.startswith("http"):
                return href
            if href:
                return "https://www.xiaohongshu.com" + href
    except Exception as e:
        log.debug("获取小红书笔记链接失败：%s", e)
    return None

"""微信视频号创作者中心（channels.weixin.qq.com）Playwright 自动化：扫码登录 + 上传发布。

注意：
- 标题是独立输入框
- 无分区概念，话题标签写进描述
- 视频号发布页可能有原创声明等弹窗，dismiss_dialogs 覆盖
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
    new_context,
    rand_sleep,
    settle,
    shot,
    wait_upload_done,
)

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "weixin_state.json"

PUBLISH_URL = "https://channels.weixin.qq.com/platform/post/create"
LOGIN_URL = "https://channels.weixin.qq.com/"
MANAGE_URL = "https://channels.weixin.qq.com/platform/post/list"

UPLOAD_TIMEOUT = 15 * 60

SELECTORS = {
    "file_input": "input[type='file'][accept*='video']",
    "title_input": [
        'input.weui-desktop-form__input[placeholder*="标题"]',
        'input[placeholder*="短标题"]',
        'input[placeholder*="标题"]',
        'textarea[placeholder*="标题"]',
    ],
    "desc_editor": [
        "div.input-editor[contenteditable]",
        "div[contenteditable][data-placeholder*='描述']",
        "div[contenteditable]",
        'textarea[placeholder*="描述"]',
        "textarea",
    ],
    "publish_btn_text": "发表",
    "upload_done_texts": ["上传完成", "上传成功", "封面已生成", "审核中", "重新上传", "封面生成中"],
    "upload_fail_texts": ["上传失败", "上传出错"],
    "success_texts": ["发布成功", "已发布", "发表成功"],
}


class WeixinError(PublishError):
    pass


def _is_logged_in(page: Page) -> bool:
    try:
        if "login" in page.url or "passport" in page.url:
            return False
        return page.locator(SELECTORS["file_input"]).count() > 0
    except Exception:
        return False


def _has_login_cookies(context) -> bool:
    """视频号创作者中心的会话 cookie。"""
    try:
        cookies = context.cookies(["https://channels.weixin.qq.com"])
        names = {c["name"] for c in cookies}
        return bool({"slave_sid", "slave_user", "video_account_id",
                     "wxid", "sessionid"} & names)
    except Exception:
        return False


# ---------- 登录 ----------

def login_interactive(state_path: Path = STATE_PATH) -> bool:
    """打开有头浏览器扫码登录微信视频号创作者中心，保存登录态。"""
    with sync_playwright() as p:
        browser = launch_chromium(p, headless=False)
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(4)
            print("\n>>> 浏览器已打开微信视频号创作者中心，请用微信扫码登录（5 分钟内有效）...\n")
            deadline = time.time() + 300
            while time.time() < deadline:
                url = page.url
                logged_in = "login" not in url and "passport" not in url
                if (logged_in and _has_login_cookies(context)) or _has_login_cookies(context):
                    time.sleep(2)
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=str(state_path))
                    print(f">>> 登录成功！登录态已保存到 {state_path}")
                    return True
                time.sleep(2)
            print(">>> 等待登录超时，未保存登录态")
            return False
        finally:
            context.close()
            browser.close()


# ---------- 发布 ----------

def publish(
    video: Path,
    title: str,
    description: str,
    tags: list[str],
    category: str | None,       # 视频号无分区，忽略
    cover: Path | None = None,  # 视频号封面来自首帧，暂不自定义
    headless: bool = True,
    state_path: Path = STATE_PATH,
) -> str | None:
    """上传并发布一条视频到微信视频号，成功返回链接（获取不到时 None）。"""
    if not Path(state_path).exists():
        raise LoginExpired("未找到视频号登录态，请先运行: python -m bot.main --login weixin")

    tag_str = " ".join(f"#{t}" for t in tags if t)
    desc_full = "\n".join(x for x in (description, tag_str) if x)

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=headless)
        context = new_context(browser, state_path)
        page = context.new_page()
        try:
            page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
            settle(page)
            if not _has_login_cookies(context):
                shot(page, "weixin_login_expired")
                raise LoginExpired("视频号登录态已失效，请运行 python -m bot.main --login weixin 重新扫码")
            dismiss_dialogs(page)

            # 确保在视频上传页（先检查 file input 是否已就绪，是则跳过 tab 切换）
            file_input = page.locator(SELECTORS["file_input"]).first
            if not file_input.count():
                _ensure_video_tab(page)
                page.wait_for_timeout(3000)

            # 1. 上传视频（file input 是隐藏的，用 attached 状态等待）
            file_input = page.locator(SELECTORS["file_input"]).first
            try:
                file_input.wait_for(state="attached", timeout=30000)
            except Exception:
                shot(page, "weixin_upload_fail")
                raise WeixinError("未找到视频上传入口，截图见 logs/")
            file_input.set_input_files(str(video))
            log.info("已提交视频上传：%s", video.name)

            # 2. 等待上传完成（视频号需要等视频处理完按钮才会启用）
            wait_upload_done(page, SELECTORS["upload_done_texts"], SELECTORS["upload_fail_texts"],
                             shot_prefix="weixin", timeout=UPLOAD_TIMEOUT)
            page.wait_for_timeout(5000)
            dismiss_dialogs(page)
            rand_sleep()

            # 3. 标题（≥6 字，视频号最低要求）
            min_title = title[:30] if len(title) >= 6 else title + "精彩视频"
            _fill_title(page, min_title)
            # 填完标题后描述框才会渲染，等待它出现
            page.wait_for_timeout(3000)

            # 4. 描述 + 话题标签
            fill_editor(page, desc_full, shot_prefix="weixin_desc",
                        candidates=[page.locator(c) for c in SELECTORS["desc_editor"]])

            # 5. 点发表
            _click_publish(page)

            weixin_url = _sanitize_weixin_url(_fetch_weixin_url(context))
            shot(page, "weixin_publish_done")
            log.info("视频号发布成功：%s", weixin_url or "（链接获取失败，见创作者中心-内容管理）")
            return weixin_url
        finally:
            context.close()
            browser.close()


def _ensure_video_tab(page: Page) -> None:
    """确保在视频上传 tab（可能有视频/图文切换）。"""
    try:
        for text in ("发表视频", "上传视频", "视频"):
            tab = page.get_by_text(text, exact=False).first
            if tab.count() and tab.is_visible():
                tab.click()
                rand_sleep(0.5, 1.0)
                log.info("已切换到视频上传 tab")
                return
    except Exception as e:
        log.debug("切换视频 tab 失败（可能无需切换）：%s", e)


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
                    log.info("已填写视频号标题（%d 字）", len(title))
                    return
            except Exception:
                continue
        time.sleep(1)
    shot(page, "weixin_title_fail")
    raise WeixinError("未找到视频号标题输入框，截图见 logs/（页面可能已改版）")


def _find_publish_button_px(page: Page) -> tuple[int, int] | None:
    """像素分析定位底部蓝色/绿色发表按钮（视频号按钮可能也在 Shadow DOM 里）。"""
    try:
        import io

        from PIL import Image
        png = page.screenshot()
        img = Image.open(io.BytesIO(png)).convert("RGB")
        w, h = img.size
        xs, ys = [], []
        # 视频号发表按钮通常在底部右侧，蓝色或绿色
        for y in range(int(h * 0.80), h):
            for x in range(int(w * 0.40), w):
                r, g, b = img.getpixel((x, y))
                # 蓝色系 (r<120, g<180, b>150) 或 绿色系 (g>150, r<120, b<120)
                if (r < 120 and g < 180 and b > 150) or (g > 150 and r < 120 and b < 120):
                    xs.append(x)
                    ys.append(y)
        if len(xs) < 50:
            return None
        xs.sort()
        ys.sort()
        return xs[len(xs) // 2], ys[len(ys) // 2]
    except Exception as e:
        log.debug("像素定位发表按钮失败：%s", e)
        return None


def _click_publish(page: Page) -> None:
    # 先关掉弹窗
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.5)
    dismiss_dialogs(page, extra_texts=("我知道了", "知道了", "确定", "同意", "原创声明"))

    # 等待发表按钮可用（视频号按钮禁用时 class 含 btn_disabled）
    deadline = time.time() + 30
    btn = None
    while time.time() < deadline:
        for text in (SELECTORS["publish_btn_text"], "发布"):
            try:
                b = page.get_by_role("button", name=text, exact=False).first
                if b.count() and b.is_visible():
                    cls = b.get_attribute("class") or ""
                    if "btn_disabled" not in cls:
                        btn = b
                        break
            except Exception:
                continue
        if btn:
            break
        time.sleep(2)

    if not btn:
        pos = _find_publish_button_px(page)
        if pos:
            log.info("像素定位到发表按钮 @(%d,%d)，物理点击", *pos)
            btn_click = lambda: page.mouse.click(*pos)
        else:
            shot(page, "weixin_publish_btn_fail")
            raise WeixinError("发表按钮不可用（可能仍有必填项未完成），截图见 logs/")
    else:
        btn_click = lambda: btn.click()

    # 注册网络响应监听：视频号发表成功返回 201 到 post 接口
    published = {"ok": False}
    def _on_response(r):
        try:
            if not published["ok"] and r.status in (200, 201) and "post" in r.url and "mmfinderassistant" in r.url:
                published["ok"] = True
        except Exception:
            pass
    page.on("response", _on_response)
    btn_click()
    if btn:
        log.info("点击发表按钮（DOM）")

    # 等待成功：优先看网络响应 201，其次看页面文字/URL 变化
    deadline = time.time() + 60
    while time.time() < deadline:
        page.wait_for_timeout(2000)  # 用 Playwright 事件循环而非 time.sleep，确保回调能执行
        if published["ok"]:
            log.info("检测到发表成功（网络响应 201）")
            return
        for text in SELECTORS["success_texts"]:
            if page.get_by_text(text, exact=False).count():
                return
        if "post/list" in page.url or "post/manage" in page.url:
            return
        # 视频号可能弹出确认弹窗
        for text in ("确认发表", "确定", "确认"):
            confirm = page.get_by_role("button", name=text, exact=False).first
            if confirm.count() and confirm.is_visible():
                confirm.click()
                log.info("点击确认发表")
                break
    shot(page, "weixin_publish_result_unknown")
    raise WeixinError("点击发表后 60 秒内未检测到成功标志，请查看 logs/ 截图确认")


def _sanitize_weixin_url(href: str | None) -> str | None:
    """协议页 / 条款页不能当作品链接。"""
    if not href or not href.startswith("http"):
        return None
    low = href.lower()
    if "weixin_agreement" in low or "readtemplate" in low:
        log.warning("视频号抓到的不是作品链接（协议页），已忽略：%s", href[:120])
        return None
    return href


def _fetch_weixin_url(context) -> str | None:
    """发布成功后到内容管理页抓最新视频链接（尽力而为）。"""
    try:
        page = context.new_page()
        page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(4000)
        link = page.locator("a[href*='finder'], a[href*='video']").first
        if link.count():
            href = link.get_attribute("href") or ""
            if href.startswith("http"):
                return href
    except Exception as e:
        log.debug("获取视频号链接失败：%s", e)
    return None

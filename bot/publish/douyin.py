"""抖音创作者中心（creator.douyin.com）Playwright 自动化：扫码登录 + 上传发布。

注意：
- 抖音发布页无「分区」概念，AI 生成的话题标签（#xxx）直接写进简介
- 标题是简介编辑器的第一行，上限 55 字（文案层已按此约束生成）
- 滑块/验证码风控：检测到验证元素立即截图报错，TG 通知人工处理
- 选择器集中在 SELECTORS，页面改版后只需调整这里
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from ..config import DATA_DIR
from .base import (
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

STATE_PATH = DATA_DIR / "douyin_state.json"

PUBLISH_URL = "https://creator.douyin.com/creator-micro/content/upload"
LOGIN_HINT = "passport.douyin.com"

UPLOAD_TIMEOUT = 15 * 60

SELECTORS = {
    "file_input": "input[type='file']",
    "editor": "div[contenteditable='true']",
    "publish_btn_text": "发布",
    "upload_done_texts": ["重新上传", "上传完成", "已上传完成"],
    "upload_fail_texts": ["上传失败", "上传出错", "上传中断"],
    "success_texts": ["发布成功", "视频已提交", "投稿成功"],
    # 滑块/验证码特征（出现任意一个即判定触发风控）
    "captcha_selectors": [
        "#captcha-verify-image",
        ".captcha_verify_container",
        "#captcha_container",
    ],
    "captcha_texts": ["拖动滑块", "请完成验证", "安全验证"],
}


class DouyinError(PublishError):
    pass


def _has_captcha(page: Page) -> bool:
    """是否出现滑块/验证码（抖音风控）。"""
    try:
        for sel in SELECTORS["captcha_selectors"]:
            if page.locator(sel).count():
                return True
        for text in SELECTORS["captcha_texts"]:
            if page.get_by_text(text, exact=False).count():
                return True
    except Exception:
        pass
    return False


def _check_captcha(page: Page, where: str) -> None:
    if _has_captcha(page):
        shot(page, "dy_captcha")
        raise DouyinError(f"抖音触发滑块/安全验证（{where}），截图见 logs/。"
                          f"请用 --login douyin 打开浏览器人工过验证，或稍后自动重试")


def _is_logged_in(page: Page) -> bool:
    try:
        if LOGIN_HINT in page.url:
            return False
        return page.locator(SELECTORS["file_input"]).count() > 0
    except Exception:
        return False


def _has_login_cookies(context) -> bool:
    try:
        cookies = context.cookies(["https://creator.douyin.com", "https://www.douyin.com"])
        names = {c["name"] for c in cookies}
        return "sessionid" in names or "sessionid_ss" in names
    except Exception:
        return False


# ---------- 登录 ----------

def _dismiss_identity_card(page: Page) -> None:
    """未登录时会先弹「我是个人创作者 / 我是机构」身份选择卡片，点掉才会出二维码。"""
    try:
        for text in ("我是个人创作者", "个人创作者", "我是创作者"):
            btn = page.get_by_text(text, exact=False)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                rand_sleep(0.5, 1.0)
                log.info("已选择身份（%s），等待二维码加载", text)
                return
    except Exception as e:
        log.debug("身份选择卡片处理失败：%s", e)


def login_interactive(state_path: Path = STATE_PATH) -> bool:
    """打开有头浏览器扫码登录，保存登录态。成功返回 True。"""
    with sync_playwright() as p:
        browser = launch_chromium(p, headless=False, slow_mo=150)
        context = new_context(browser, state_path)
        page = context.new_page()
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
        settle(page)
        _dismiss_identity_card(page)
        print("\n>>> 浏览器已打开抖音创作者中心，请用抖音 App 扫码登录（5 分钟内有效）...")
        print(">>> 如弹出滑块验证请手动完成；登录成功后会自动保存登录态\n")
        deadline = time.time() + 300
        while time.time() < deadline:
            if _has_login_cookies(context):
                time.sleep(3)  # 等登录跳转把 cookie 补齐
                state_path.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(state_path))
                print(f">>> 登录成功！登录态已保存到 {state_path}")
                browser.close()
                return True
            time.sleep(2)
        print(">>> 等待登录超时，未保存登录态")
        browser.close()
        return False


# ---------- 发布 ----------

def publish(
    video: Path,
    title: str,
    description: str,
    tags: list[str],
    category: str | None,       # 抖音无分区，忽略
    cover: Path | None = None,  # 抖音封面编辑复杂，暂用平台自动封面
    headless: bool = True,
    state_path: Path = STATE_PATH,
    manual_verify: bool = False,  # True=有头模式，短信验证弹窗由人工在窗口里完成
) -> str | None:
    """上传并发布一条视频，成功返回作品链接（获取不到时 None）。"""
    if not Path(state_path).exists():
        raise LoginExpired("未找到抖音登录态，请先运行: python -m bot.main --login douyin")

    # 标题(≤55字) + 简介 + 话题标签，都写进同一个编辑器；话题用 # 引出
    tag_str = " ".join(f"#{t}" for t in tags if t)
    content = "\n".join(x for x in (title, description, tag_str) if x)

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=headless)
        context = new_context(browser, state_path)
        page = context.new_page()
        try:
            page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
            settle(page)
            _check_captcha(page, "打开发布页")
            if not (_is_logged_in(page) or _has_login_cookies(context)):
                shot(page, "dy_login_expired")
                raise LoginExpired("抖音登录态已失效，请运行 python -m bot.main --login douyin 重新扫码")

            dismiss_dialogs(page)

            # 1. 上传视频
            file_input = page.locator(SELECTORS["file_input"]).first
            file_input.set_input_files(str(video))
            log.info("已提交视频上传：%s", video.name)

            # 2. 等待上传完成（抖音上传后即进入编辑表单）
            wait_upload_done(page, SELECTORS["upload_done_texts"], SELECTORS["upload_fail_texts"],
                             shot_prefix="dy", timeout=UPLOAD_TIMEOUT)
            page.wait_for_timeout(5000)
            _check_captcha(page, "上传后")
            dismiss_dialogs(page)
            rand_sleep()

            # 3. 填标题+简介+话题（抖音为同一个 contenteditable 编辑器）
            editor = page.locator(SELECTORS["editor"]).first
            fill_editor(page, content, shot_prefix="dy_desc", candidates=[editor])

            # 4. 点发布（manual_verify=True 时短信验证弹窗由人工完成）
            _click_publish(page, manual_verify=manual_verify)

            dy_url = _fetch_dy_url(context)
            shot(page, "dy_publish_done")
            log.info("抖音发布成功：%s", dy_url or "（作品链接获取失败，见创作者中心-内容管理）")
            return dy_url
        finally:
            context.close()
            browser.close()


def _sms_dialog_present(page: Page) -> bool:
    """是否弹出「接收短信验证码」风控弹窗（发布被拦，等人工输入验证码）。"""
    try:
        for text in ("接收短信验证码", "请输入当前手机号", "短信验证码"):
            if page.get_by_text(text, exact=False).count():
                return True
    except Exception:
        pass
    return False


def _click_publish(page: Page, manual_verify: bool = False) -> None:
    candidates = [
        page.get_by_role("button", name=SELECTORS["publish_btn_text"], exact=True),
        page.locator("button", has_text=re.compile(r"^\s*发\s*布\s*$")),
        page.get_by_text(SELECTORS["publish_btn_text"], exact=True),
    ]
    for loc in candidates:
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.click()
                break
        except Exception:
            continue
    else:
        shot(page, "dy_publish_btn_fail")
        raise DouyinError("未找到发布按钮，截图见 logs/（抖音页面可能已改版）")

    deadline = time.time() + 60
    sms_prompted = False
    while time.time() < deadline:
        if _has_captcha(page):
            shot(page, "dy_captcha_on_publish")
            raise DouyinError("抖音在点击发布时触发滑块验证，截图见 logs/，请人工过验证或稍后重试")
        if _sms_dialog_present(page):
            if manual_verify:
                deadline = time.time() + 300  # 人工输入验证码，等 5 分钟
                if not sms_prompted:
                    sms_prompted = True
                    shot(page, "dy_sms_verify")
                    log.info("抖音要求短信验证，请在浏览器窗口输入验证码并点击「验证」（5 分钟内有效）")
                # 等用户输入验证码，弹窗消失后继续等成功标志
                time.sleep(2)
                continue
            shot(page, "dy_sms_verify")
            raise DouyinError("抖音发布触发短信验证（风控），需人工在浏览器里输入验证码完成发布；"
                              "自动流程已中止，本条会重试")
        for text in SELECTORS["success_texts"]:
            loc = page.get_by_text(text, exact=False)
            if loc.count() and loc.first.is_visible():
                return
        if "/content/manage" in page.url:
            return
        time.sleep(2)
    shot(page, "dy_publish_result_unknown")
    raise DouyinError("点击发布后 60 秒内未检测到成功标志，请查看 logs/ 截图确认")


def _fetch_dy_url(context) -> str | None:
    """发布成功后到内容管理页抓最新作品链接（尽力而为）。"""
    try:
        page = context.new_page()
        page.goto("https://creator.douyin.com/creator-micro/content/manage",
                  wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(4000)
        link = page.locator("a[href*='/video/']").first
        if link.count():
            href = link.get_attribute("href") or ""
            if href.startswith("http"):
                return href
            if href:
                return "https://www.douyin.com" + href
    except Exception as e:
        log.debug("获取抖音作品链接失败：%s", e)
    return None

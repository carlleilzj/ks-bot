"""今日头条 / 头条号（mp.toutiao.com）Playwright 自动化：扫码登录 + 上传发布。

选择器与页面流程对齐已在生产里跑通的 toutiao-ops
（mf-yang/toutiao-ops cli/src/publish-video.js、auth.js），不靠猜：

- 登录页 https://mp.toutiao.com/auth/page/login ，扫今日头条 App
- 已登录落在 /profile_v4 ，未登录会跳 /auth/page/login 或 sso.toutiao.com
- 视频上传页 https://mp.toutiao.com/profile_v4/xigua/upload-video
- 标题框 placeholder「请输入 1～30 个字符」，硬限 30 字（含前缀）
- 话题框 placeholder 精确等于「请输入」（和标题框不是同一个）
- 简介 textarea placeholder「请输入视频简介」
- 封面：点「上传封面」→「本地上传」，失败再退回截帧
- 发布按钮用 button:has-text("发布") 的最后一个，避免点到别的「发布」
- 作品声明在「高级设置」里，标签全文以 toutiao-ops 的 labelMap 为准

头条号登录态是否会像视频号那样服务端短 TTL，要等第一次真实发布后才知道。
这里只在 cookie 真变化时回写 storage_state，不靠写文件续期。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from ..config import DATA_DIR, LOGS_DIR
from .base import (
    UA,
    LoginExpired,
    PublishError,
    dismiss_dialogs,
    launch_chromium,
    new_context,
    persist_state_if_changed,
    rand_sleep,
    settle,
    shot,
)

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "toutiao_state.json"

HOME_URL = "https://mp.toutiao.com/"
LOGIN_URL = "https://mp.toutiao.com/auth/page/login"
PUBLISH_URL = "https://mp.toutiao.com/profile_v4/xigua/upload-video"

LOGIN_PATH = "/auth/page/login"
DASHBOARD_PATH = "/profile_v4"

# 标题硬限。前缀【有点视频】占 6 字，钩子只剩 24 字，发布前再切一刀。
TITLE_LIMIT = 30

# 字节 SSO 常见登录 cookie。URL 才是权威判据（toutiao-ops 也只看 URL），
# 这些名字只用来打日志、以及「人在后台但 cookie 还没落盘」时多等一会儿。
_LOGIN_COOKIE_NAMES = {
    "sessionid", "sessionid_ss", "sid_tt", "uid_tt", "passport_csrf_token",
}

# 作品声明：短名 → 页面上的完整文案。键和 toutiao-ops setDeclarations 一致。
DECLARATION_LABELS = {
    "取自站外": "取自站外",
    "引用站内": "引用站内",
    "自行拍摄": "自行拍摄",
    "AI生成": "AI生成",
    "虚构演绎": "虚构演绎，故事经历",
    "投资观点": "投资观点，仅供参考",
    "健康医疗": "健康医疗分享，仅供参考",
}

_QR_SELECTORS = (
    "[class*='qrcode']", "[class*='qr-code']", "[class*='QRCode']",
    "[class*='qr_code']", "[class*='web-login']", "[class*='scan']",
)


class ToutiaoError(PublishError):
    pass


def fit_title(title: str, limit: int = TITLE_LIMIT) -> str:
    """压到头条标题上限。中文按字符计，和输入框「1～30 个字符」一致。"""
    t = re.sub(r"\s+", " ", (title or "").strip())
    if len(t) <= limit:
        return t
    return t[:limit].rstrip()


def declaration_label(raw: str | None) -> str:
    """配置里的短名换成页面上的完整声明文案；未知字符串原样返回。"""
    key = (raw or "").strip()
    if not key:
        return ""
    return DECLARATION_LABELS.get(key, key)


def is_login_url(url: str) -> bool:
    u = url or ""
    return LOGIN_PATH in u or "sso.toutiao.com" in u


def is_dashboard_url(url: str) -> bool:
    u = url or ""
    return DASHBOARD_PATH in u and not is_login_url(u)


def _is_logged_in(page: Page) -> bool:
    return is_dashboard_url(page.url or "")


def _cookie_names(context, urls: list[str] | None = None) -> set[str]:
    try:
        cookies = context.cookies(urls) if urls else context.cookies()
        return {c.get("name", "") for c in cookies}
    except Exception:
        return set()


def _has_login_cookies(context) -> bool:
    names = _cookie_names(context, ["https://mp.toutiao.com", "https://www.toutiao.com"])
    return bool(names & _LOGIN_COOKIE_NAMES)


def _wait_login_settled(page: Page, context, rounds: int = 5) -> bool:
    """URL 已进后台后，再给 cookie 几秒落盘。URL 到了就算登录完成。"""
    for _ in range(rounds):
        if _is_logged_in(page) and _has_login_cookies(context):
            return True
        time.sleep(2)
    return _is_logged_in(page)


def _save_login_state(context, state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(state_path))


# ---------- 登录 ----------

def login_interactive(state_path: Path = STATE_PATH, timeout_sec: int = 300) -> bool:
    """有头浏览器扫码登录。本地有桌面时用。"""
    with sync_playwright() as p:
        browser = launch_chromium(p, headless=False)
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = context.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            print("\n>>> 请用今日头条 App 扫描二维码登录头条号")
            print(f">>> 等待登录（{timeout_sec}s）。成功后自动保存登录态\n")
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                if _wait_login_settled(page, context, rounds=1):
                    _save_login_state(context, state_path)
                    names = _cookie_names(context)
                    print(f">>> 登录成功！登录态已保存到 {state_path}（{len(names)} 个 cookie）")
                    return True
                time.sleep(2)
            print(">>> 等待登录超时，未保存登录态")
            return False
        finally:
            context.close()
            browser.close()


def login_qr_image(out_path: Path | None = None,
                   state_path: Path = STATE_PATH,
                   wait_sec: int = 300) -> bool:
    """无头登录：截二维码推到 Telegram，手机头条 App 扫，成功后保存登录态。

    头条二维码大约 50 秒失效（toutiao-ops 按 50 秒刷新）。过期就重载重截重发。
    """
    out_path = out_path or (LOGS_DIR / "toutiao_qr.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _push_tg(caption: str) -> None:
        try:
            from ..config import load_settings
            from ..notify import telegram
            telegram.send_photo(load_settings(), out_path, caption)
            log.info("头条登录二维码已推送到 Telegram")
        except Exception as e:
            log.debug("Telegram 推送失败（不影响登录）：%s", str(e)[:100])

    def _grab_qr(page: Page, path: Path) -> bool:
        for sel in _QR_SELECTORS:
            loc = page.locator(sel)
            if not loc.count():
                continue
            try:
                if loc.first.is_visible():
                    loc.first.screenshot(path=str(path))
                    return True
            except Exception:
                continue
        try:
            page.screenshot(path=str(path))
        except Exception:
            return False
        return False

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=True)
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(4)
            if is_dashboard_url(page.url or ""):
                _save_login_state(context, state_path)
                print(f">>> 头条号已是登录态，已保存到 {state_path}")
                return True

            _grab_qr(page, out_path)
            print(f"\n>>> 二维码已导出：{out_path}")
            print(">>> 请用今日头条 App 扫码登录头条号，登录成功后自动保存登录态\n")
            _push_tg("🟢 今日头条登录二维码（约 50 秒有效，过期会自动刷新重发）\n"
                     "用今日头条 App 扫码登录头条号，登录成功后自动保存登录态")

            deadline = time.time() + wait_sec
            next_refresh = time.time() + 50
            refreshes = 0
            while time.time() < deadline:
                if _is_logged_in(page):
                    if _wait_login_settled(page, context):
                        _save_login_state(context, state_path)
                        names = _cookie_names(context)
                        print(f">>> 登录成功！登录态已保存到 {state_path}"
                              f"（{len(names)} 个 cookie）")
                        return True
                if time.time() >= next_refresh and refreshes < 5:
                    refreshes += 1
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=20000)
                    except Exception:
                        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(3)
                    if _grab_qr(page, out_path):
                        _push_tg(f"🟢 今日头条二维码已刷新（第 {refreshes} 次），请重新扫")
                    next_refresh = time.time() + 50
                time.sleep(2)
            print(">>> 等待登录超时，未保存登录态")
            return False
        finally:
            context.close()
            browser.close()


# ---------- 发布 ----------

def _wait_upload(page: Page, timeout: int = 10 * 60) -> None:
    """等上传完成。toutiao-ops 的判据：正文出现「上传成功」，或标题框已经出来。"""
    deadline = time.time() + timeout
    last_log = 0.0
    title_sel = 'input[placeholder*="字符"]'
    while time.time() < deadline:
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        if "上传失败" in body:
            shot(page, "toutiao_upload_fail")
            raise ToutiaoError("视频上传失败（页面出现「上传失败」），截图见 logs/")
        if "上传成功" in body:
            log.info("头条上传完成（上传成功）")
            return
        try:
            if page.locator(title_sel).count():
                log.info("头条上传完成（标题框已出现）")
                return
        except Exception:
            pass
        if time.time() - last_log > 30:
            log.info("等待头条上传与转码...（最长 %d 分钟）", timeout // 60)
            last_log = time.time()
        time.sleep(3)
    shot(page, "toutiao_upload_timeout")
    raise ToutiaoError(f"等待头条上传超时（{timeout // 60} 分钟），截图见 logs/")


def _fill_title(page: Page, title: str) -> None:
    box = page.locator('input[placeholder="请输入 1～30 个字符"]').first
    try:
        box.wait_for(state="visible", timeout=20000)
    except Exception:
        box = page.locator('input[placeholder*="字符"]').first
        box.wait_for(state="visible", timeout=8000)
    text = fit_title(title)
    if not text:
        raise ToutiaoError("标题为空，头条标题要求 1～30 个字符")
    box.click(force=True)
    box.fill("")
    rand_sleep(0.2, 0.4)
    page.keyboard.type(text, delay=40)
    log.info("头条标题（%d 字）：%s", len(text), text)


def _add_topic(page: Page, topic: str) -> None:
    """话题搜索框 placeholder 精确是「请输入」，点开后选第一条建议。失败不挡发布。"""
    topic = (topic or "").lstrip("#").strip()
    if not topic:
        return
    field = page.locator('input[placeholder="请输入"]').first
    try:
        if not field.count():
            log.info("未找到话题输入框，跳过话题")
            return
        field.click(force=True, timeout=5000)
        rand_sleep(0.3, 0.5)
        page.keyboard.type(topic, delay=50)
        rand_sleep(0.8, 1.2)
        suggestion = page.locator(
            '[class*="option"], [class*="suggest"], [class*="topic"]'
        ).first
        try:
            suggestion.click(timeout=4000)
        except Exception:
            page.keyboard.press("Enter")
        log.info("已尝试添加话题：%s", topic)
    except Exception as e:
        log.warning("话题添加失败（不影响发布）：%s", str(e)[:120])


def _fill_description(page: Page, description: str, tags: list[str]) -> None:
    tag_str = " ".join(f"#{t.lstrip('#')}" for t in tags if t)
    text = "\n".join(x for x in ((description or "").strip(), tag_str) if x)
    if not text:
        return
    box = page.locator('textarea[placeholder="请输入视频简介"]').first
    try:
        if not box.count():
            log.info("未找到视频简介框，跳过简介")
            return
        box.click(force=True, timeout=5000)
        box.fill(text[:400])
        log.info("已填写视频简介（%d 字）", min(len(text), 400))
    except Exception as e:
        log.warning("简介填写失败（不影响发布）：%s", str(e)[:120])


def _click_visible_ok(page: Page) -> bool:
    """从最上层开始点可见的「确定」。封面流程有两层确认。"""
    locs = page.locator('text="确定"')
    try:
        n = locs.count()
    except Exception:
        return False
    for i in range(n - 1, -1, -1):
        btn = locs.nth(i)
        try:
            if btn.is_visible():
                btn.click(force=True, timeout=5000)
                return True
        except Exception:
            continue
    return False


def _upload_local_cover(page: Page, cover: Path) -> None:
    tab = page.locator("text=本地上传").first
    tab.click(timeout=5000)
    rand_sleep(0.6, 1.0)
    file_input = page.locator("input[type='file'][accept*='image']")
    if not file_input.count():
        file_input = page.locator("input[type='file']")
    # 第一个 file input 是视频的，封面对话框里的在后面
    target = file_input.last if file_input.count() else None
    if target is None or not file_input.count():
        raise ToutiaoError("封面对话框里没有文件输入框")
    target.set_input_files(str(cover))
    rand_sleep(1.5, 2.5)
    nxt = page.locator('button:has-text("下一步"), text=下一步').first
    try:
        if nxt.count() and nxt.is_visible():
            nxt.click(timeout=5000)
            rand_sleep(1.0, 1.6)
    except Exception:
        pass
    _click_visible_ok(page)
    rand_sleep(0.8, 1.2)
    _click_visible_ok(page)


def _use_frame_cover(page: Page) -> None:
    nxt = page.locator("text=下一步").first
    if not nxt.count() or not nxt.is_visible():
        log.info("封面截取没有「下一步」，跳过")
        return
    nxt.click(force=True, timeout=5000)
    rand_sleep(1.2, 1.8)
    _click_visible_ok(page)
    rand_sleep(0.8, 1.2)
    _click_visible_ok(page)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if not page.locator("text=封面编辑").first.is_visible():
                break
        except Exception:
            break
        time.sleep(1)


def _set_cover(page: Page, cover: Path | None) -> None:
    trigger = page.locator("text=上传封面").first
    try:
        if not trigger.count():
            log.warning("未找到「上传封面」，跳过封面（发布时若被拦，截图会留下）")
            return
        trigger.click(timeout=5000)
    except Exception as e:
        log.warning("点开封面对话框失败：%s", str(e)[:120])
        return
    rand_sleep(1.0, 1.6)
    try:
        if cover and Path(cover).exists():
            _upload_local_cover(page, Path(cover))
            log.info("已上传自定义封面：%s", Path(cover).name)
        else:
            _use_frame_cover(page)
            log.info("未提供封面文件，改用截帧")
    except Exception as e:
        log.warning("自定义封面失败，改用截帧：%s", str(e)[:140])
        try:
            _use_frame_cover(page)
        except Exception as e2:
            log.warning("截帧封面也失败（继续尝试发布）：%s", str(e2)[:140])
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    rand_sleep(0.3, 0.5)


def _expand_advanced(page: Page) -> None:
    toggle = page.locator("text=高级设置").first
    try:
        if not toggle.count():
            return
        toggle.scroll_into_view_if_needed()
    except Exception:
        return
    already = False
    for marker in ("作品声明", "选择合集", "谁可以看"):
        try:
            loc = page.locator(f"text={marker}").first
            if loc.count() and loc.is_visible():
                already = True
                break
        except Exception:
            continue
    if already:
        return
    try:
        toggle.click(timeout=5000)
        rand_sleep(0.6, 1.0)
    except Exception as e:
        log.warning("展开高级设置失败：%s", str(e)[:100])


def _set_declaration(page: Page, declaration: str | None) -> None:
    label = declaration_label(declaration)
    if not label:
        return
    _expand_advanced(page)
    try:
        box = page.locator(f"text={label}").first
        box.scroll_into_view_if_needed()
        box.click(timeout=4000)
        log.info("已勾选作品声明：%s", label)
    except Exception as e:
        log.warning("作品声明未勾上（不影响发布）：%s", str(e)[:120])


def _click_publish(page: Page) -> None:
    dismiss_dialogs(page, extra_texts=("我知道了", "知道了", "同意", "取消"))
    btn = page.locator('button:has-text("发布")').last
    try:
        btn.wait_for(state="visible", timeout=15000)
        btn.scroll_into_view_if_needed()
        rand_sleep(0.3, 0.5)
        btn.click(force=True, timeout=10000)
    except Exception:
        shot(page, "toutiao_publish_btn_fail")
        raise ToutiaoError("未点到「发布」按钮，截图见 logs/")


# 上传页上一直都有这些字（「正常预计审核完成时间」里就含「审核完成」），
# 不能拿它们当发布成功。2026-09-30 第一条因此被记成 PUBLISHED，作品列表仍是空的。
_SUCCESS_MARKERS = ("发布成功", "发表成功", "提交成功")
_BLOCK_MARKERS = (
    "该账号信息未完善",
    "完善后才能发布视频",
    "请完善账号信息",
)


def account_blocked_reason(body: str) -> str | None:
    """账号未完善时头条允许填表，但发布不会进作品列表。"""
    text = body or ""
    for marker in _BLOCK_MARKERS:
        if marker in text:
            return marker
    return None


def publish_succeeded(body: str, url: str, start_url: str) -> bool:
    """必须离开上传页，或成功文案出现时已经不在上传页。停在原页一律不算。"""
    here = url or ""
    still_uploading = "upload-video" in here
    left = bool(here) and here != (start_url or "") and not still_uploading and is_dashboard_url(here)
    if left:
        return True
    if still_uploading:
        return False
    return any(marker in (body or "") for marker in _SUCCESS_MARKERS)


def _wait_published(page: Page, timeout: int = 25) -> str | None:
    """点发布后必须离开上传页。还停在上传页就当失败，并留下截图。"""
    deadline = time.time() + timeout
    start_url = page.url or ""
    while time.time() < deadline:
        url = page.url or ""
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        blocked = account_blocked_reason(body)
        if blocked:
            shot(page, "toutiao_account_blocked")
            raise ToutiaoError(
                f"头条号未完善（页面出现「{blocked}」），发布不会入库。"
                "请用今日头条 App 打开：我的 → 设置 → 扫一扫，完成个人账号认证")
        if publish_succeeded(body, url, start_url):
            log.info("头条已离开上传页：%s", (url or "")[:120])
            return url
        if "发布失败" in body:
            shot(page, "toutiao_publish_rejected")
            raise ToutiaoError("头条拒绝发布（页面出现「发布失败」），截图见 logs/")
        time.sleep(1)
    shot(page, "toutiao_publish_still")
    raise ToutiaoError("点击发布后仍停在上传页，不能记为成功，截图见 logs/")


def publish(
    video: Path,
    title: str,
    description: str,
    tags: list[str],
    category: str | None = None,  # 头条视频发布页没有分区，忽略
    cover: Path | None = None,
    headless: bool = True,
    state_path: Path = STATE_PATH,
    declaration: str | None = None,
) -> str | None:
    """上传并发布一条视频到头条号。成功返回页面 URL（拿不到作品链接时 None）。"""
    if not Path(state_path).exists():
        raise LoginExpired("未找到头条号登录态，请先运行: python -m bot.main --login-qr toutiao")

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=headless)
        context = new_context(browser, state_path)
        page = context.new_page()
        try:
            page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
            settle(page)
            # 上传页就在 /profile_v4 下。未登录会被踢到登录页或 SSO，不在后台即失效。
            if not is_dashboard_url(page.url or ""):
                shot(page, "toutiao_login_expired")
                raise LoginExpired("头条号登录态已失效，请重新扫码登录")
            try:
                blocked = account_blocked_reason(page.inner_text("body"))
            except Exception:
                blocked = None
            if blocked:
                shot(page, "toutiao_account_blocked")
                raise ToutiaoError(
                    f"头条号未完善（页面出现「{blocked}」），发布不会入库。"
                    "请用今日头条 App 打开：我的 → 设置 → 扫一扫，完成个人账号认证")
            dismiss_dialogs(page)

            file_input = page.locator("input[type='file']").first
            try:
                file_input.wait_for(state="attached", timeout=30000)
            except Exception:
                shot(page, "toutiao_upload_entry_fail")
                raise ToutiaoError("未找到视频上传入口，截图见 logs/")
            file_input.set_input_files(str(video))
            log.info("已提交视频上传：%s", Path(video).name)

            _wait_upload(page)
            rand_sleep(1.0, 1.6)
            dismiss_dialogs(page)

            _fill_title(page, title)
            if tags:
                _add_topic(page, tags[0])
            _set_cover(page, cover)
            _fill_description(page, description, tags)
            if declaration:
                _set_declaration(page, declaration)

            _click_publish(page)
            return _wait_published(page)
        finally:
            try:
                persist_state_if_changed(
                    context, Path(state_path),
                    domains=["toutiao.com", "bytedance.com"])
            except Exception as e:
                log.debug("头条登录态回写跳过：%s", str(e)[:80])
            context.close()
            browser.close()

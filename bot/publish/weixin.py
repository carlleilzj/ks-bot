"""微信视频号创作者中心（channels.weixin.qq.com）Playwright 自动化：扫码登录 + 上传发布。

注意：
- 标题是独立输入框
- 无分区概念，话题标签写进描述
- 视频号发布页可能有原创声明等弹窗，dismiss_dialogs 覆盖
- 选择器集中在 SELECTORS，页面改版后只需调整这里
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
    fill_editor,
    launch_chromium,
    new_context,
    persist_state_if_changed,
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
    """判定当前页面是否已登录视频号助手。

    2026-09-17 修正两处误判（登录成功后实测发现）：
      1. 旧代码用 `[class*='qrcode']` 找二维码——这个选择器在**已登录**的
         后台页面上也能命中 3 个元素（框架残留节点），导致恒判未登录。
         改为只在明确的登录容器里找，且要求元素可见。
      2. URL 判据 `"/platform/" in url` 漏掉不带尾斜杠的 `/platform`
         （登录后重定向到的正是它）。改为前缀匹配。
    """
    try:
        url = page.url.lower()
        if "login" in url or "passport" in url:
            return False
        for t in ("登录视频号助手", "扫码登录", "请使用微信扫码"):
            if page.get_by_text(t, exact=False).count():
                return False
        # 可见的登录二维码才算未登录（排除后台页面的框架残留节点）
        if page.locator("img.qrcode:visible, .qrcode-img:visible").count():
            return False
        # 已登录的强信号：后台 URL 或上传用文件输入框
        if url.startswith("https://channels.weixin.qq.com/platform"):
            return True
        return page.locator(SELECTORS["file_input"]).count() > 0
    except Exception:
        return False


def _has_login_cookies(context) -> bool:
    """视频号创作者中心是否已下发会话 cookie（用于发布前的可用性判断）。

    实测（2026-09-29）：视频号完整登录态就是 sessionid + wxuin 两个 cookie。
    登录落盘请用 _login_complete()（额外要求 wxuin 非空，避免存下半个会话）。
    """
    try:
        cookies = context.cookies(["https://channels.weixin.qq.com"])
        names = {c["name"] for c in cookies}
        return bool({"slave_sid", "slave_user", "video_account_id",
                     "wxid", "sessionid"} & names)
    except Exception:
        return False


def _login_complete(context) -> bool:
    """登录态是否可用（可以安全落盘）。

    2026-09-29 实测更正：视频号**只设 2 个 cookie**（sessionid + wxuin），
    没有 slave_sid/slave_user/video_account_id —— 起初按「≥3 个」判定，
    是错的，只会让登录流程白等 20 秒。真正的凭据就是 sessionid 本身。

    所以这里只要求 sessionid 非空。wxuin 一并校验，因为它标识账号身份。
    """
    try:
        cookies = context.cookies(["https://channels.weixin.qq.com"])
        vals = {c["name"]: (c.get("value") or "") for c in cookies}
        return bool(vals.get("sessionid")) and bool(vals.get("wxuin"))
    except Exception:
        return False


def login_qr_image(out_path: Path | None = None,
                   state_path: Path = STATE_PATH,
                   wait_sec: int = 300) -> bool:
    """无头环境下登录：把二维码导成图片 + 轮询等待扫码，成功后保存登录态。

    用途：发布 worker 跑在阿里云（无桌面），没法弹有头浏览器扫码。
    这个函数用无头浏览器打开登录页 → 截出二维码区域存成 PNG →
    把 PNG 推给 Telegram，用手机微信扫 → 轮询到登录成功即保存 state。

    二维码会过期，所以带自动刷新：检测到失效就重载页面重新截码并重发 TG，
    否则人拿到图时码已经灰了（快手那边实测过这个坑）。

    返回是否登录成功。
    """
    out_path = out_path or (LOGS_DIR / "weixin_qr.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _push_tg(caption: str) -> None:
        """推二维码到 TG；失败只记日志，不阻断登录（文件照样落盘可 scp）。"""
        try:
            from ..config import load_settings
            from ..notify import telegram
            telegram.send_photo(load_settings(), out_path, caption)
            log.info("二维码已推送到 Telegram")
        except Exception as e:
            log.debug("Telegram 推送失败（不影响登录）：%s", str(e)[:100])

    def _grab_qr(page, path: Path) -> bool:
        """截二维码元素；找不到退化为整页截图。"""
        for sel in ("img.qrcode", ".qrcode-img", "[class*='qrcode'] img",
                    "[class*='qrcode']", "canvas"):
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

    def _qr_expired(page) -> bool:
        """页面出现「已失效/已过期/点击刷新」即为码过期。"""
        for t in ("二维码已失效", "二维码已过期", "已失效", "点击刷新", "重新获取"):
            try:
                if page.get_by_text(t, exact=False).count():
                    return True
            except Exception:
                continue
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
            time.sleep(6)
            _grab_qr(page, out_path)
            print(f"\n>>> 二维码已导出：{out_path}")
            print(">>> 请用微信扫码登录视频号助手，登录成功后自动保存登录态\n")
            _push_tg("🟢 视频号登录二维码（几分钟内有效，过期会自动刷新重发）\n"
                     "用微信扫码登录视频号助手，登录成功后自动保存登录态")

            deadline = time.time() + wait_sec
            next_check = 0.0
            refreshes = 0
            while time.time() < deadline:
                if _is_logged_in(page) or _has_login_cookies(context):
                    # 等 cookie 稳定再落盘（sessionid + wxuin 两个即完整，
                    # 实测视频号就只设这两个；见 _login_complete）
                    for _ in range(5):
                        if _login_complete(context):
                            break
                        time.sleep(2)
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=str(state_path))
                    n = len(context.cookies(["https://channels.weixin.qq.com"]))
                    log.info("视频号登录成功，登录态已保存：%s（%d 个 cookie）",
                             state_path, n)
                    print(f">>> 登录成功！登录态已保存到 {state_path}（{n} 个 cookie）"
                          + (f"（中途自动刷新 {refreshes} 次）" if refreshes else ""))
                    return True

                # 每 12 秒查一次是否过期，过期就重载页面重新截码重发
                now = time.time()
                if now >= next_check:
                    next_check = now + 12
                    try:
                        if _qr_expired(page):
                            refreshes += 1
                            log.info("二维码已过期，第 %d 次刷新", refreshes)
                            page.reload(wait_until="domcontentloaded", timeout=45000)
                            time.sleep(6)
                            _grab_qr(page, out_path)
                            _push_tg(f"🔄 视频号登录二维码（已刷新 {refreshes} 次，请扫这张）\n"
                                     "用微信扫码登录视频号助手")
                    except Exception as e:
                        log.debug("刷新二维码失败：%s", str(e)[:100])
                time.sleep(2)

            log.warning("等待扫码超时（%ds），未保存登录态", wait_sec)
            print(">>> 等待扫码超时，未保存登录态")
            return False
        finally:
            context.close()
            browser.close()


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
                    for _ in range(10):      # 等完整登录态落地，见 _login_complete
                        if _login_complete(context):
                            break
                        time.sleep(2)
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=str(state_path))
                    n = len(context.cookies(["https://channels.weixin.qq.com"]))
                    print(f">>> 登录成功！登录态已保存到 {state_path}（{n} 个 cookie）")
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
            if not _has_login_cookies(context) or not _is_logged_in(page):
                shot(page, "weixin_login_expired")
                raise LoginExpired("视频号登录态已失效，请重新扫码登录")
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

            # 3. 短标题（视频号硬性要求：6~16 字且符号受限）
            min_title = _sanitize_short_title(title)
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
            # 回写登录态：捕获交互中服务端可能下发的 cookie 更新。
            # 实测更正（2026-09-29）：sessionid 在页面访问后**不轮换**，
            # 所以这不是「续期」——会话过期由服务端 TTL 决定（客户端 expires
            # 到 2027 也没用）。真正的防线是 remote_api 不再把登录失效的 job
            # 打成终态，以及这里只在 cookie 真变化时才落盘（避免用死状态
            # 覆盖好文件、刷新 mtime 造成「刚更新过」的假象）。
            persist_state_if_changed(context, state_path,
                                     domains=["channels.weixin.qq.com"])
            context.close()
            browser.close()


def _sanitize_short_title(title: str) -> str:
    """视频号短标题规范：
    - 字数严格限制 6~16 字（超限或不足发布按钮均被禁用）
    - 符号仅支持书名号、引号、冒号、加号、问号、百分号、摄氏度，逗号可用空格代替
    - 坚决不可使用【】方括号、感叹号等，一律清理或替换
    """
    t = (title or "").strip()
    # 替换方括号、圆括号为纯空格
    t = re.sub(r'[【】\[\]()（）]', ' ', t)
    # 逗号、句号、感叹号一律替换为空格
    t = re.sub(r'[,，.。!！;；~～·\-_/]', ' ', t)
    # 移除非法符号（仅保留中英文数字、空格及官方允许的标点：书名号《》、引号""‘’“”、冒号:：、加号+、问号?？、百分号%、摄氏度℃）
    t = re.sub(r'[^\w\s《》“”"\'’‘+?？%:：℃]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    if len(t) > 16:
        parts = t.split()
        if parts and 6 <= len(parts[0]) <= 16:
            t = parts[0]
        else:
            t = t[:16].strip()
    if len(t) < 6:
        t = (t + " 治愈解压")[:16].strip()
    return t


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
                    page.keyboard.press("Tab")
                    page.wait_for_timeout(1000)
                    log.info("已填写视频号标题（%d 字）：%s", len(title), title)
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
    # 1. 向下滚动确保发表按钮渲染并位于可视区
    for _ in range(5):
        page.mouse.wheel(0, 800)
        page.wait_for_timeout(300)
    page.wait_for_timeout(1000)

    # 2. 先关掉可能存在的弹窗
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.5)
    dismiss_dialogs(page, extra_texts=("我知道了", "知道了", "确定", "同意", "原创声明", "取消", "关闭"))

    # 3. 等待发表按钮可用（视频号按钮禁用时 class 含 btn_disabled）
    deadline = time.time() + 35
    btn = None
    while time.time() < deadline:
        for text in (SELECTORS["publish_btn_text"], "发布"):
            try:
                b = page.get_by_role("button", name=text, exact=False).first
                if b.count():
                    b.scroll_into_view_if_needed()
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
        btn_click = lambda: btn.click(force=True)

    btn_click()
    if btn:
        log.info("点击发表按钮（DOM）")

    # 注册网络响应监听：视频号发表成功返回 200/201（过滤掉普通上报接口）
    published = {"ok": False}
    def _on_response(r):
        try:
            path = r.url.split("?")[0]
            if not published["ok"] and r.status in (200, 201) and any(
                p in path for p in ("/post/create", "/post/create_post", "/post/publish", "/post/post_create")
            ):
                published["ok"] = True
        except Exception:
            pass
    page.on("response", _on_response)

    # 等待成功：优先看网络响应 200/201，其次看页面文字/URL 变化，循环中持续检测并点击二次确认弹窗
    deadline = time.time() + 60
    while time.time() < deadline:
        page.wait_for_timeout(1000)

        # 视频号原创声明/分成计划二次确认弹窗：“直接发表”或“确定”
        for text in ("直接发表", "确认发表", "确定", "确认", "继续发表"):
            try:
                confirm = page.get_by_role("button", name=text, exact=False).first
                if confirm.count() and confirm.is_visible():
                    confirm.click(force=True)
                    log.info("点击了发布确认弹窗按钮：%s", text)
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        if published["ok"]:
            log.info("检测到发表成功（网络响应 200/201）")
            page.wait_for_timeout(3000)
            return

        for text in SELECTORS["success_texts"]:
            if page.get_by_text(text, exact=False).count():
                log.info("检测到发表成功文字标志：%s", text)
                page.wait_for_timeout(3000)
                return

        if "post/list" in page.url or "post/manage" in page.url:
            log.info("检测到已跳转至作品列表：%s", page.url)
            page.wait_for_timeout(3000)
            return

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

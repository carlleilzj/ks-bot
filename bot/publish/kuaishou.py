"""快手创作者中心（cp.kuaishou.com）Playwright 自动化：登录态管理 + 上传发布。

公共工具（context/截图/弹窗/上传轮询）复用 publish.base；
快手特有的选择器与流程集中在本文件，前端改版后只需调整 SELECTORS。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from ..config import DATA_DIR, LOGS_DIR, KS_STATE_PATH
from .base import (
    LoginExpired,
    PublishError,
    _remove_joyride,
    dismiss_dialogs,
    fill_editor,
    launch_chromium,
    new_context,
    rand_sleep,
    settle,
    shot,
    wait_upload_done,
    goto_with_retry,
    reload_with_retry,
)

log = logging.getLogger(__name__)

PUBLISH_URL = "https://cp.kuaishou.com/article/publish/video"
MANAGE_URL = "https://cp.kuaishou.com/article/manage"
SPARK_RR_PATH = DATA_DIR / "ks_spark_rr.json"
SPARK_TYPE_LABEL = "关联变现任务"
# 发布页「作者服务」区两个下拉的占位符（2026-09-16 实机核对）
SPARK_TYPE_PLACEHOLDER = "选择服务类型"
# 注意：平台文案改过。旧版是「关联变现任务获得更多收入」，
# 2026-09 起实际页面显示「关联成功可获得更多收益」。两版都带上以兼容。
SPARK_TASK_PLACEHOLDERS = ("关联成功可获得更多收益", "关联变现任务获得更多收入")

# 转码等待上限（秒）
UPLOAD_TIMEOUT = 15 * 60

SELECTORS = {
    "file_input": "input[type='file']",
    "cover_input": "input[type='file'][accept*='image']",
    "desc_editor": [
        "div[contenteditable='true']",
        "textarea[placeholder*='简介']",
        "textarea[placeholder*='作品']",
        "textarea",
    ],
    "category_entry_text": "选择分类",
    "publish_btn_text": "发布",
    "upload_done_texts": ["上传完成", "转码完成"],
    "upload_fail_texts": ["上传失败", "上传出错"],
    "success_texts": ["发布成功", "提交成功", "审核中"],
}

# 快手发布页描述框（动态渲染，多个候选按序尝试）
_DESC_CANDIDATES = [
    '#work-description-edit',
    'div[contenteditable="true"]',
    '[class*="description"][contenteditable="true"]',
    'textarea[placeholder*="简介"]',
    'textarea',
]


class KuaishouError(PublishError):
    pass


class SparkAttachError(PublishError):
    """星火变现任务挂载未生效。

    与「不阻断发布」的其他可选步骤（分区/封面）不同：星火挂载失败意味着
    作品拿不到播放奖金池收益，属于静默收益损失，必须阻断发布而不是放行。
    """
    pass


def _fill_desc_js(page: Page, text: str) -> bool:
    """JS 直接注入描述（不点击元素，绕过 joyride 引导遮罩的点击拦截）。

    快手发布页的 react-joyride 空引导会拦截对表单的点击，点击后表单还可能被销毁；
    用 execCommand 注入文本可触发 React 的受控输入事件，效果等同手动输入。
    """
    try:
        ok = page.evaluate("""([text]) => {
            const el = document.querySelector('#work-description-edit');
            if (!el) return false;
            el.focus();
            // 清空旧内容
            el.textContent = '';
            document.execCommand('insertText', false, text);
            return true;
        }""", [text])
        if not ok:
            return False
        # 验证内容真的进去了
        page.wait_for_timeout(800)
        got = page.evaluate("() => (document.querySelector('#work-description-edit')||{}).textContent || ''")
        return bool(got.strip())
    except Exception as e:
        log.debug("JS 注入描述失败：%s", str(e)[:100])
        return False


def _desc_visible(page: Page) -> bool:
    """任一描述框候选存在于 DOM（被 joyride 遮罩覆盖时 is_visible 返回 False，但 CSS 已禁用 pointer-events，点击实际可达）。"""
    for sel in _DESC_CANDIDATES:
        try:
            loc = page.locator(sel).first
            if loc.count():
                return True
        except Exception:
            continue
    return False


def _joyride_present(page: Page) -> bool:
    try:
        return bool(page.evaluate(
            "() => !!document.querySelector('#react-joyride-portal, .react-joyride__overlay, .react-joyride__spotlight')"))
    except Exception:
        return False


def _wait_form_ready(page: Page, timeout: float = 40.0) -> None:
    """上传完成后等发布表单就绪：反复关弹窗，直到描述框在 DOM 中。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        dismiss_dialogs(page)
        _remove_joyride(page)  # CSS 注入屏蔽 joyride 点击拦截
        if _desc_visible(page):
            return
        page.wait_for_timeout(1500)
    shot(page, "ks_form_not_ready")
    raise KuaishouError("上传完成后 40 秒内发布表单未就绪（可能有弹窗遮挡或页面改版），截图见 logs/")


# ---------- 登录态判定 ----------

def _wait_file_input_with_retry(page: Page, max_refresh: int = 3) -> None:
    """等上传输入框出现；遇到页面加载失败自动刷新重试。

    快手创作者页偶发前端加载失败（主内容区出不来 input[type=file]），
    刷新即可恢复。不处理的话 wait_for_selector 60s 超时 → 误报失败/误判登录失效。
    """
    for refresh in range(max_refresh + 1):
        try:
            page.wait_for_selector(SELECTORS["file_input"], state="attached", timeout=20_000)
            return
        except Exception:
            pass
        if refresh < max_refresh:
            log.warning("发布页加载失败（第 %d 次），自动刷新重试", refresh + 1)
            shot(page, f"ks_page_broken_{refresh + 1}")
            reload_with_retry(page)
            page.wait_for_timeout(5000)
    page.wait_for_selector(SELECTORS["file_input"], state="attached", timeout=60_000)


def _wait_ks_upload_done(page: Page, timeout: int = UPLOAD_TIMEOUT) -> None:
    """等待快手视频真正上传完成（直到「上传中」消失，且无失败提示）。"""
    deadline = time.time() + timeout
    upload_started = False
    last_log = 0.0

    while time.time() < deadline:
        # 1. 检查失败
        for fail in SELECTORS["upload_fail_texts"]:
            if page.get_by_text(fail, exact=False).count():
                shot(page, "ks_upload_fail")
                raise PublishError(f"快手视频上传失败（页面出现「{fail}」）")

        # 2. 检查上传状态与进度
        status = page.evaluate("""() => {
            const body = document.body.innerText || '';
            const lines = body.split('\\n').map(s => s.trim()).filter(Boolean);
            const hasUploading = lines.includes('上传中') || body.includes('上传中');
            const matchPercent = body.match(/\\b(\\d{1,3})%\\b/);
            return {
                hasUploading,
                percent: matchPercent ? matchPercent[1] : null
            };
        }""")

        if status["hasUploading"]:
            upload_started = True
            if time.time() - last_log > 15:
                pct_str = f"（{status['percent']}%）" if status["percent"] else ""
                log.info("等待快手视频上传与转码%s...（最长 %d 分钟）", pct_str, timeout // 60)
                last_log = time.time()
        else:
            if upload_started:
                log.info("快手视频上传完成")
                page.wait_for_timeout(2000)
                return
            # 尚未捕获到上传中（可能视频较小瞬间传完，或者刚开始）：等待最多 10 秒
            if time.time() - (deadline - timeout) > 10:
                log.info("快手视频上传完成（未处于上传状态）")
                return

        page.wait_for_timeout(2000)

    shot(page, "ks_upload_timeout")
    raise PublishError(f"等待快手视频上传超时（{timeout // 60} 分钟）")


def _upload_with_retry(page: Page, video, max_attempts: int = 3, timeout: int = UPLOAD_TIMEOUT) -> None:
    """上传视频；网络抖动导致的上传失败/超时自动重传。"""
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            _wait_file_input_with_retry(page)
            file_input = page.locator(SELECTORS["file_input"]).first
            file_input.set_input_files(str(video))
            log.info("已提交视频上传：%s（第 %d 次）", video.name, attempt)

            _wait_ks_upload_done(page, timeout=timeout)
            return
        except PublishError as e:
            last_err = e
            if attempt >= max_attempts:
                break
            log.warning("上传失败（第 %d/%d 次）：%s，准备重传", attempt, max_attempts, str(e)[:120])
            try:
                goto_with_retry(page, PUBLISH_URL)
                page.wait_for_timeout(5000)
                dismiss_dialogs(page)
            except Exception as e2:
                log.warning("重传前刷新页面失败：%s", str(e2)[:100])
    raise last_err if last_err else PublishError("视频上传失败（重传耗尽）")


def _is_logged_in(page: Page) -> bool:
    """发布页上的快速判定：不在登录页且能找到上传入口。

    2026-09-19 修正：作品管理页（/article/manage/*）没有文件上传入口，
    旧逻辑在那里恒判 False（cookie 实际有效）。补两条管理页判据：
    URL 前缀 + 页面出现「作品管理」字样。
    """
    try:
        if "passport.kuaishou.com" in page.url:
            return False
        url = page.url
        if url.startswith(("https://cp.kuaishou.com/article/manage",
                           "https://cp.kuaishou.com/article/publish")):
            return True
        if page.locator(SELECTORS["file_input"]).count() > 0:
            return True
        # 兜底：管理页正文里有「作品管理」导航
        try:
            body = page.locator("body").inner_text(timeout=5_000)
            if "作品管理" in body or "共" and "个作品" in body:
                return True
        except Exception:
            pass
        return False
    except Exception:
        return False


def _cookie_file_has_login(state_path: Path) -> bool:
    """直接读 storage_state 文件判登录 cookie（不依赖 Playwright 域匹配）。

    实测 context.cookies(["https://cp.kuaishou.com"]) 只返回 4 个 cookie，
    查不到 .kuaishou.com / id.kuaishou.com 域的 userId/passToken，
    导致登录态有效却被误判失效 → job 跳过。
    另：userId 存在多条（有过期的有有效的），须按 expires 过滤。
    """
    import time as _time
    try:
        data = json.loads(Path(state_path).read_text())
        now = _time.time()
        names = set()
        for c in data.get("cookies", []):
            exp = c.get("expires", -1)
            # expires=-1 是会话 cookie 视为有效；有过期时间的必须未过期
            if exp is None or exp < 0 or exp > now:
                names.add(c.get("name", ""))
        return "userId" in names or "passToken" in names
    except Exception:
        return False


def _has_login_cookies(context) -> bool:
    """按 cookie 判定登录态（不依赖页面结构，扫码成功即生效）。"""
    try:
        cookies = context.cookies(["https://cp.kuaishou.com", "https://www.kuaishou.com",
                                    "https://id.kuaishou.com", "https://passport.kuaishou.com"])
        names = {c["name"] for c in cookies}
        if "userId" in names or "passToken" in names:
            return True
    except Exception:
        pass
    # 域匹配查不到时兜底：直接读 storage_state 文件
    return _cookie_file_has_login(KS_STATE_PATH)


# ---------- 登录 ----------

def login_interactive(state_path: Path = KS_STATE_PATH) -> bool:
    """打开有头浏览器扫码登录，保存登录态。成功返回 True。"""
    with sync_playwright() as p:
        browser = launch_chromium(p, headless=False, slow_mo=150)
        context = new_context(browser, state_path)
        page = context.new_page()
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
        print("\n>>> 浏览器已打开快手创作者中心，请扫码登录（5 分钟内有效）...")
        print(">>> 登录成功后会自动保存登录态，之后日常运行无需再次扫码\n")
        deadline = time.time() + 300
        while time.time() < deadline:
            if _has_login_cookies(context):
                time.sleep(2)  # 等登录跳转把 cookie 补齐
                state_path.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(state_path))
                print(f">>> 登录成功！登录态已保存到 {state_path}")
                browser.close()
                return True
            time.sleep(2)
        print(">>> 等待登录超时，未保存登录态")
        browser.close()
        return False


def login_qr_image(out_path: Path | None = None, state_path: Path = KS_STATE_PATH,
                   wait_sec: int = 300) -> bool:
    """无头环境二维码登录：截图二维码 + 轮询等待扫码，成功即存登录态。

    为什么需要这个：login_interactive 需要 headless=False 弹有头浏览器，
    在服务器（无桌面）上跑不起来。快手登录态过期后只能人工上机，
    实测 HK B 上的快手登录态已失效（2026-09-19），作品管理页跳回登录页。

    产物：logs/kuaishou_qr.png（可直接发到手机扫）
    """
    if out_path is None:
        out_path = LOGS_DIR / "kuaishou_qr.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=True)
        context = new_context(browser, state_path)
        page = context.new_page()
        try:
            page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
            settle(page)
            page.wait_for_timeout(8000)

            # 若已登录，直接保存并返回
            if _has_login_cookies(context) or _is_logged_in(page):
                context.storage_state(path=str(state_path))
                print(f">>> 检测到已登录，登录态已刷新：{state_path}")
                browser.close()
                return True

            # 未登录时 PUBLISH_URL 会落到**营销落地页**（不是登录页），
            # 页面上没有二维码，只有一个「立即登录」按钮，必须点进去。
            # 实测（2026-09-19）：不点它，截出来的只有平台介绍页，无码可扫。
            for label in ("立即登录", "登录/注册", "登录"):
                btn = page.get_by_text(label, exact=True)
                if btn.count():
                    try:
                        btn.first.click(timeout=8000)
                        log.info("已点击「%s」，等待跳转到登录页", label)
                        page.wait_for_timeout(6000)
                        break
                    except Exception as e:
                        log.debug("点击「%s」失败：%s", label, str(e)[:80])

            # 登录页默认是**密码登录**（手机号+密码输入框），
            # 必须先切到「扫码登录」标签才会显示二维码。
            # 实测（2026-09-19）：不切标签，截图里只有密码输入框。
            for label in ("扫码登录", "二维码登录"):
                tab = page.get_by_text(label, exact=True)
                if tab.count():
                    try:
                        tab.first.click(timeout=8000)
                        log.info("已切到「%s」标签", label)
                        page.wait_for_timeout(5000)
                        break
                    except Exception as e:
                        log.debug("切换「%s」失败：%s", label, str(e)[:80])

            # 找二维码：优先常见选择器（要求可见），找不到就截整个视口
            qr = None
            for sel in ("img.qrcode", ".qrcode-img", "[class*='qrcode'] img",
                        "[class*='qr-code']", "img[src*='qr']",
                        "[class*='qrcode']", "canvas"):
                loc = page.locator(sel)
                if not loc.count():
                    continue
                try:
                    if loc.first.is_visible():
                        qr = loc.first
                        log.info("定位到二维码元素：%s", sel)
                        break
                except Exception:
                    continue

            if qr is not None:
                try:
                    qr.screenshot(path=str(out_path))
                    log.info("二维码已导出：%s", out_path)
                except Exception as e:
                    log.warning("二维码元素截图失败（%s），改截全页", str(e)[:80])
                    page.screenshot(path=str(out_path))
            else:
                page.screenshot(path=str(out_path))
                log.info("未定位到二维码元素，已截全页：%s", out_path)

            print(f"\n=== 快手 二维码登录（最长等 {wait_sec}s，码过期自动刷新）===")
            print(f">>> 二维码已导出：{out_path}")
            print(">>> 请用快手 App 扫码登录创作者中心，登录成功后自动保存登录态")

            # 直接推送到 Telegram：人不在电脑前也能扫（手机上看图扫码最快）。
            # 失败不阻断登录流程 —— 文件照样落盘，仍可 scp 拉取。
            try:
                from ..config import load_settings
                from ..notify import telegram
                tg = load_settings()
                telegram.send_photo(
                    tg, out_path,
                    "🟠 快手登录二维码（几分钟内有效，过期会自动刷新重发）\n"
                    "用快手 App 扫码登录创作者中心")
                log.info("二维码已推送到 Telegram")
            except Exception as e:
                log.debug("Telegram 推送失败（不影响登录）：%s", str(e)[:100])

            # ---- 扫码等待循环（带过期自动刷新）----
            # 背景（2026-09-19 实测）：快手服务端的码只活 2~3 分钟，
            # 用户拿到图再扫经常已经灰了。与其反复人肉喊"重新生成"，
            # 不如在这里自动检测失效并刷新，把新图持续覆盖到 out_path，
            # 谁负责展示（scp/Telegram）谁就总能拿到最新的。
            expired_streak = 0
            refreshes = 0
            deadline = time.time() + wait_sec
            next_check = 0.0
            while time.time() < deadline:
                if _has_login_cookies(context):
                    time.sleep(2)   # 等登录跳转把 cookie 补齐
                    context.storage_state(path=str(state_path))
                    print(f"\n>>> 登录成功！登录态已保存到 {state_path}"
                          + (f"（中途自动刷新 {refreshes} 次）" if refreshes else ""))
                    browser.close()
                    return True

                # 每 15 秒查一次是否出现「已失效」灰层
                now = time.time()
                if now >= next_check:
                    next_check = now + 15
                    try:
                        expired = page.evaluate("""() => {
                            const t = document.body ? document.body.innerText : '';
                            return /二维码已失效|二维码过期|已过期|请刷新|刷新重试/.test(t);
                        }""")
                    except Exception:
                        expired = False

                    if expired:
                        expired_streak += 1
                        # 找「刷新」按钮 / 点击二维码区域重出码
                        refreshed = False
                        for sel_txt in ("刷新", "点击刷新", "刷新重试", "重新获取"):
                            btn = page.get_by_text(sel_txt, exact=False)
                            if btn.count():
                                try:
                                    btn.first.click(timeout=5000)
                                    refreshed = True
                                    break
                                except Exception:
                                    continue
                        if not refreshed and qr is not None:
                            # 没有文字按钮时，点二维码本身通常也会触发刷新
                            try:
                                qr.click(timeout=5000)
                                refreshed = True
                            except Exception:
                                pass
                        if refreshed:
                            page.wait_for_timeout(4000)
                            try:
                                if qr is not None:
                                    qr.screenshot(path=str(out_path))
                                else:
                                    page.screenshot(path=str(out_path))
                                refreshes += 1
                                expired_streak = 0
                                log.info("二维码已失效，已自动刷新（第 %d 次），新图已覆盖 %s",
                                         refreshes, out_path)
                                # 每次刷新都重推 Telegram，保证手机上是新码
                                try:
                                    telegram.send_photo(
                                        tg, out_path,
                                        f"♻️ 二维码已过期并自动刷新（第 {refreshes} 次），"
                                        "请扫这张新图")
                                except Exception:
                                    pass
                            except Exception as e:
                                log.warning("刷新后重新截图失败：%s", str(e)[:80])
                        else:
                            # 实在刷不动：整页重载兜底
                            if expired_streak >= 2:
                                log.warning("刷新按钮不可用，整页重载重出码")
                                try:
                                    page.reload(wait_until="domcontentloaded")
                                    page.wait_for_timeout(8000)
                                    for label in ("立即登录", "登录/注册", "登录"):
                                        b2 = page.get_by_text(label, exact=True)
                                        if b2.count():
                                            b2.first.click(timeout=6000)
                                            page.wait_for_timeout(5000)
                                            break
                                    for label in ("扫码登录", "二维码登录"):
                                        t2 = page.get_by_text(label, exact=True)
                                        if t2.count():
                                            t2.first.click(timeout=6000)
                                            page.wait_for_timeout(4000)
                                            break
                                    for sel in ("img.qrcode", ".qrcode-img",
                                                "[class*='qrcode'] img",
                                                "[class*='qrcode']"):
                                        loc = page.locator(sel)
                                        if loc.count() and loc.first.is_visible():
                                            loc.first.screenshot(path=str(out_path))
                                            qr = loc.first
                                            refreshes += 1
                                            expired_streak = 0
                                            log.info("整页重载后重新导出二维码（第 %d 次）", refreshes)
                                            break
                                except Exception as e:
                                    log.warning("整页重载失败：%s", str(e)[:80])

                time.sleep(2)

            print(f"\n>>> 等待登录超时（{wait_sec}s）"
                  + (f"，中途刷新 {refreshes} 次" if refreshes else "")
                  + "，未保存登录态")
            browser.close()
            return False
        except Exception as e:
            log.warning("快手二维码登录异常：%s", str(e)[:150])
            try:
                browser.close()
            except Exception:
                pass
            return False


# ---------- 发布 ----------

def publish(
    video: Path,
    title: str,
    description: str,
    tags: list[str],
    category: str | None,
    cover: Path | None = None,
    headless: bool = True,
    state_path: Path = KS_STATE_PATH,
    spark_task: bool = False,
    spark_task_title: str = "",
) -> str | None:
    """上传并发布一条视频，成功返回快手作品链接（获取不到时 None）。"""
    if not Path(state_path).exists():
        raise LoginExpired("未找到快手登录态，请先运行: python -m bot.main --login kuaishou")

    tag_str = " ".join(f"#{t}" for t in tags[:4] if t)  # 快手话题上限 4 个，超出提交会被拒
    desc_full = "\n".join(x for x in (title, description, tag_str) if x)

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=headless)
        context = new_context(browser, state_path)
        page = context.new_page()
        try:
            goto_with_retry(page, PUBLISH_URL)
            settle(page)
            if not (_is_logged_in(page) or _has_login_cookies(context)):
                shot(page, "ks_login_expired")
                raise LoginExpired("快手登录态已失效，请运行 python -m bot.main --login kuaishou 重新扫码")

            # 0. 关闭「继续编辑上次未发布视频」弹窗（如果有）
            dismiss_dialogs(page)

            # 1+2. 上传视频并等待转码完成（网络抖动导致失败自动重传）
            _upload_with_retry(page, video)
            # 上传完成后表单异步渲染，且可能弹出「继续编辑上次未发布视频」等弹窗：
            # 循环关弹窗 + 等描述框可见，最多 40 秒
            _wait_form_ready(page)

            # 3. 填写标题/简介/标签（快手为同一个描述框）。
            #    先试 JS 注入（避开 joyride 点击拦截），失败再退回常规 fill_editor
            if not _fill_desc_js(page, desc_full):
                fill_editor(page, desc_full, shot_prefix="ks_desc",
                            candidates=[page.locator(c) for c in _DESC_CANDIDATES])

            # 4. 选择分区（可选，失败不阻断）
            if category:
                _select_category(page, category)

            # 5. 上传自定义封面（可选，失败不阻断）
            if cover and Path(cover).exists():
                _upload_cover(page, Path(cover))

            # 5.5 挂星火「关联变现任务」（需 App 先收藏；失败**阻断发布**）
            #     与分区/封面不同，星火挂载失败 = 作品拿不到奖金池收益，
            #     属静默收益损失，故此处必须硬失败：
            #     SparkAttachError → worker 回报失败 → job 回 PENDING 待重试。
            #     重试无解时（平台侧任务下架/额度满）人工介入：
            #     去 App 重新收藏任务，或把 config.yaml 的 spark_task 设为 false。
            if spark_task:
                attached = _attach_spark_task(page, prefer=spark_task_title)
                if not attached:
                    shot(page, "ks_spark_blocked")
                    raise SparkAttachError(
                        "星火变现任务挂载未生效，已阻断发布（避免无收益白发）。"
                        "请检查：快手 App 星火计划里任务是否仍可挂、额度是否已满、"
                        "或把 config.yaml 的 spark_task 设为 false 以跳过")

            # 6. 点击发布
            _click_publish(page, title=title)

            ks_url = _fetch_ks_url(context)
            shot(page, "ks_publish_done")
            log.info("发布成功：%s", ks_url or "（作品链接获取失败，见创作者中心-内容管理）")
            return ks_url
        finally:
            context.close()
            browser.close()


def _select_category(page: Page, category: str) -> None:
    try:
        entry = page.get_by_text(SELECTORS["category_entry_text"], exact=False).first
        if not (entry.count() and entry.is_visible()):
            log.warning("未找到「选择分类」入口，跳过分区选择")
            return
        entry.click()
        rand_sleep()
        item = page.get_by_text(category, exact=True).first
        if item.count():
            item.click()
            rand_sleep(0.3, 0.8)
            for btn_text in ("确定", "完成", "收起"):
                btn = page.get_by_role("button", name=btn_text, exact=True)
                if btn.count():
                    btn.first.click()
                    break
            log.info("已选择分区：%s", category)
        else:
            log.warning("分区弹层中未找到「%s」，跳过分区选择（可核对 config.yaml 的 platform_categories）", category)
            page.keyboard.press("Escape")
    except Exception as e:
        log.warning("选择分区失败（不影响发布）：%s", e)
        shot(page, "ks_category_fail")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass


def pick_spark_title(titles: list[str], prefer: str = "", cursor: int = 0) -> tuple[str, int]:
    """从收藏任务标题里选一个：prefer 子串优先，否则按 cursor 轮询。返回 (标题, 下一个 cursor)。"""
    clean = [t.strip() for t in titles if t and t.strip()]
    if not clean:
        return "", cursor
    if prefer:
        for t in clean:
            if prefer in t:
                return t, cursor
    i = cursor % len(clean)
    return clean[i], cursor + 1


def _spark_rr_load() -> int:
    try:
        return int(json.loads(SPARK_RR_PATH.read_text(encoding="utf-8")).get("i", 0) or 0)
    except Exception:
        return 0


def _spark_rr_save(cursor: int) -> None:
    try:
        SPARK_RR_PATH.parent.mkdir(parents=True, exist_ok=True)
        SPARK_RR_PATH.write_text(json.dumps({"i": cursor}), encoding="utf-8")
    except Exception:
        pass


def _dropdown_option_texts(page: Page) -> list[str]:
    """当前展开的 antd 下拉里的可见选项文本。

    只取 rc-virtual-list 里真正可见的 `.ant-select-item-option`，
    排除一份 height:0 的无障碍镜像（否则会读到重复/错位的文本）。
    """
    try:
        return page.evaluate("""() => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            return [...document.querySelectorAll(
                '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option'
            )].filter(e => e.offsetParent || e.getClientRects().length)
              .map(e => norm(
                  e.querySelector('.ant-select-item-option-content')?.textContent
                  ?? e.textContent))
              .filter(Boolean);
        }""") or []
    except Exception:
        return []


def _click_visible_option(page: Page, text: str) -> bool:
    """点开着的 ant-select 下拉里标题完全匹配的项。

    antd 的虚拟列表（rc-virtual-list）里可见项是 `.ant-select-item-option`，
    但同一份 DOM 里还有一份 height:0 的无障碍镜像（[role=option]），
    因此这里只认带 ant-select-item-option 类的可见节点。
    """
    try:
        ok = page.evaluate("""(want) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const nodes = [...document.querySelectorAll(
                '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option'
            )].filter(e => e.offsetParent || e.getClientRects().length);
            const el = nodes.find(e => norm(e.textContent) === String(want || '').trim())
                    || nodes.find(e => norm(e.textContent).includes(String(want || '').trim()));
            if (!el) return false;
            el.scrollIntoView({block: 'nearest'});
            // antd 选中走 mousedown+mouseup+click 三连，只发 click 有时不生效
            for (const t of ['mousedown', 'mouseup', 'click']) {
                el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
            }
            return true;
        }""", text)
        return bool(ok)
    except Exception:
        return False


def _open_select_dropdown(page: Page, placeholder: str = "") -> list[str]:
    """展开一个 antd Select 并返回它当前可见的选项文本。

    antd 的 Select 靠 **mousedown** 展开，Playwright 的 click() 只发
    click 事件，经常打不开下拉（这就是 2026-09-13 起星火静默失效的
    直接原因：下拉没展开 → 选项列表为空 → 直接 return None 跳过挂载）。

    这里直接在真实 `.ant-select-selector` 上派发 mousedown/mouseup/click，
    再回读可见选项。placeholder 为空时作用于页面上第一个可用的 select。
    """
    try:
        page.evaluate("""(ph) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const cands = [...document.querySelectorAll('.ant-select')].filter(
                e => !(e.className || '').toString().includes('disabled'));
            let target = null;
            if (ph) {
                target = cands.find(e => norm(
                    e.querySelector('.ant-select-selection-placeholder')?.textContent) === ph)
                    || cands.find(e => norm(e.textContent).includes(ph));
            } else {
                target = cands[0];
            }
            if (!target) return false;
            const box = target.querySelector('.ant-select-selector') || target;
            for (const t of ['mousedown', 'mouseup', 'click']) {
                box.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
            }
            return true;
        }""", placeholder)
        # 等浮层渲染（虚拟列表首帧可能空）
        for _ in range(10):
            page.wait_for_timeout(300)
            opts = _dropdown_option_texts(page)
            if opts:
                return opts
        return []
    except Exception as e:
        log.warning("展开下拉失败（placeholder=%r）：%s", placeholder, e)
        return []


def _click_placeholder(page: Page, placeholder: str) -> bool:
    loc = page.get_by_text(placeholder, exact=True)
    try:
        if loc.count():
            loc.last.click(force=True, timeout=2500)
            return True
    except Exception:
        pass
    return False


def _spark_form_state(page: Page) -> dict:
    """读回发布表单里星火字段的真实状态（不依赖我们是否"点成功"）。

    返回 {"service_type":..., "task":..., "picks":[...]}：
      service_type —— 「选择服务类型」那一栏当前显示的文本（未选时是占位符）
      task         —— 具体变现任务那一栏当前显示的文本（选中后是任务名，如「狐缘山间」）
      picks        —— 所有 antd 选中项的文本列表

    关键：这是**回读表单**，而非相信交互动作的返回值。2026-09-13 起出现过
    交互全部成功、日志报「已挂星火变现任务」、但平台端作品不带「流量助推」
    的静默失效——只有回读才能证伪。
    """
    try:
        return page.evaluate("""() => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const out = {service_type: '', task: '', picks: []};
            const picks = [...document.querySelectorAll('.ant-select-selection-item')]
                .map(e => norm(e.textContent)).filter(Boolean);
            out.picks = picks;
            // 作者服务区第一个下拉 = 服务类型；紧随其后那个 = 具体任务
            for (const t of picks) {
                if (!out.service_type && t === '关联变现任务') { out.service_type = t; continue; }
                if (!out.task && t !== '关联变现任务') { out.task = t; break; }
            }
            return out;
        }""") or {}
    except Exception as e:
        log.warning("回读星火表单状态失败：%s", e)
        return {}


def _spark_title_probe(page: Page, text: str) -> str:
    """去掉平台追加的「任务时间：…」后缀，得到纯任务名。"""
    return re.split(r"任务时间[:：]", (text or "").strip())[0].strip()


def _spark_attached(page: Page, chosen: str, pre_state: dict | None = None) -> bool:
    """判定星火任务是否真的挂上了：表单回读里必须出现所选任务标题。

    pre_state 为挂载前回读的表单状态（可选）。传入时会做"变化检测"：
    只有当任务栏与挂载前不同、且不再是占位文案时，才在无法按标题匹配时放行。
    这样能挡住「任务栏本来就填着别的任务」被误判为挂载成功的情况。
    """
    st = _spark_form_state(page)
    picks = st.get("picks") or []
    key = _spark_title_probe(page, chosen)
    if key:
        # 下拉项形如「狐缘山间任务时间：2026.05.06-2027.05.31」，
        # 选中后表单里显示为「狐缘山间」——所以两个方向都要能匹配上。
        for p in picks:
            p_clean = _spark_title_probe(page, p)
            if not p_clean or p_clean == SPARK_TYPE_LABEL:
                continue  # 跳过服务类型那一栏本身
            if p_clean == key or key[:6] in p_clean or p_clean[:6] in key:
                return True
        task_field = (st.get("task") or "").strip()
        if task_field and (key == task_field or key[:6] in task_field):
            return True
        # 标题没匹配上 —— 不放行。宁可阻断发布也不要挂着错任务白发。
        return False

    # 没有明确标题时（chosen 为空），退化为"任务栏非空、非占位、且相对挂载前有变化"
    task_field = (st.get("task") or "").strip()
    if not task_field or len(task_field) <= 3:
        return False
    if any(ph in task_field for ph in SPARK_TASK_PLACEHOLDERS):
        return False  # 还是占位文案 → 没挂上
    if pre_state is not None:
        before = (pre_state.get("task") or "").strip()
        if before and before == task_field:
            return False  # 与挂载前一致 → 说明这次没改动
    return True


def _select_spark_option(page: Page, chosen: str) -> bool:
    """在打开的下拉里点选任务标题。成功只代表点击动作发出，不代表已生效。"""
    if _click_visible_option(page, chosen):
        return True
    loc = page.get_by_text(chosen, exact=True)
    if loc.count():
        try:
            loc.last.click(force=True, timeout=2500)
            return True
        except Exception:
            return False
    # 平台可能给标题追加了后缀（如「任务时间：...」），用子串前缀兜底
    probe = chosen[:10]
    try:
        ok = page.evaluate("""(probe) => {
            const nodes = [...document.querySelectorAll(
                '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option'
            )];
            const el = nodes.find(e => ((e.textContent || '').trim()).includes(probe));
            if (!el) return false;
            el.scrollIntoView({block: 'nearest'});
            el.click();
            return true;
        }""", probe)
        return bool(ok)
    except Exception:
        return False


def _attach_spark_task(page: Page, prefer: str = "") -> str | None:
    """发布表单挂星火「关联变现任务」。

    返回挂载成功的任务标题；返回 None 表示**未生效**（调用方必须阻断发布）。
    注意：本函数不再"失败不阻断"——星火挂载失败 = 白丢收益，必须停下。
    """
    try:
        dismiss_dialogs(page)
        _remove_joyride(page)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(400)

        pre_state = _spark_form_state(page)  # 挂载前快照，用于变化检测

        # ── 第一层下拉：作者服务 → 关联变现任务 ──
        # 必须用 _open_select_dropdown（派发 mousedown）。antd Select 靠
        # mousedown 展开，早先用的 Playwright click() 打不开下拉，
        # 导致选项列表恒为空 → 静默跳过挂载（2026-09-13 起的线上问题）。
        opts = _open_select_dropdown(page, SPARK_TYPE_PLACEHOLDER)
        if not opts:
            log.warning("星火挂载失败：点不开「%s」下拉或下拉为空", SPARK_TYPE_PLACEHOLDER)
            shot(page, "ks_spark_no_entry")
            return None
        if SPARK_TYPE_LABEL not in opts:
            log.warning("星火挂载失败：作者服务下拉里没有「%s」，实际选项=%s",
                        SPARK_TYPE_LABEL, opts)
            shot(page, "ks_spark_no_type")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return None
        if not _click_visible_option(page, SPARK_TYPE_LABEL):
            log.warning("星火挂载失败：点不开「%s」", SPARK_TYPE_LABEL)
            shot(page, "ks_spark_type_click_fail")
            return None
        page.wait_for_timeout(1200)

        # ── 第二层下拉：具体变现任务 ──
        # 占位符文案平台改过（旧：「关联变现任务获得更多收入」，
        # 新：2026-09 起为「关联成功可获得更多收益」），两版都试。
        titles: list[str] = []
        matched_ph = ""
        for ph in SPARK_TASK_PLACEHOLDERS:
            titles = _open_select_dropdown(page, ph)
            if titles:
                matched_ph = ph
                break
        if not titles:
            log.warning("星火挂载失败：任务下拉打不开或收藏任务列表为空"
                        "（试过占位符 %s）—— 请到快手 App 星火计划收藏任务",
                        list(SPARK_TASK_PLACEHOLDERS))
            shot(page, "ks_spark_empty_list")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return None
        log.info("星火任务下拉已展开（占位符「%s」）：%d 个候选 %s",
                 matched_ph, len(titles), titles[:5])

        chosen, nxt = pick_spark_title(titles, prefer=prefer, cursor=_spark_rr_load())
        if not chosen:
            log.warning("星火挂载失败：未能从 %d 个收藏任务中选出标题", len(titles))
            shot(page, "ks_spark_pick_fail")
            return None
        if not _select_spark_option(page, chosen):
            log.warning("星火挂载失败：任务「%s」在下拉里点不到", chosen)
            shot(page, "ks_spark_option_click_fail")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return None

        # 关键：回读表单确认真的挂上了。交互成功 ≠ 平台已接受
        # （2026-09-13 起出现"日志报成功但作品无流量助推"的静默失效）
        page.wait_for_timeout(900)
        if not _spark_attached(page, chosen, pre_state=pre_state):
            log.warning("星火挂载未生效：点选「%s」后表单回读未确认（详见 ks_spark_verify_fail 截图）",
                        chosen)
            shot(page, "ks_spark_verify_fail")
            return None

        _spark_rr_save(nxt)
        page.wait_for_timeout(600)
        log.info("已挂星火变现任务（已回读校验）：%s", chosen)
        return chosen
    except Exception as e:
        log.warning("挂星火变现任务异常：%s", e)
        shot(page, "ks_spark_attach_fail")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return None


def _upload_cover(page: Page, cover: Path) -> None:
    """上传选优封面的 4:3 / 1280×960 裁切版。

    快手官方提示不低于 1280×960；直接塞 720×1280 的 9:16 会弹「应用成功」
    但信息流仍用视频帧。必须写到 file input，并用页面文案回读。
    """
    from ..media.ffmpeg import prepare_platform_covers

    try:
        variants = prepare_platform_covers(cover)
        img = variants.get("landscape") or cover
        img_input = page.locator(SELECTORS["cover_input"]).first
        opened = img_input.count() > 0
        if not opened:
            for text in ("编辑封面", "修改封面", "更换封面", "设置封面", "上传封面"):
                btn = page.get_by_text(text, exact=False).first
                if not (btn.count() and btn.is_visible()):
                    continue
                btn.click()
                rand_sleep()
                up = page.get_by_text("上传封面", exact=False).first
                if up.count() and up.is_visible():
                    up.click()
                    rand_sleep()
                img_input = page.locator(SELECTORS["cover_input"]).first
                if img_input.count():
                    opened = True
                    break
        if not opened or not img_input.count():
            log.info("未找到封面上传入口，平台将用视频帧")
            shot(page, "ks_cover_no_input")
            return
        img_input.set_input_files(str(img))
        rand_sleep(1.2, 2.0)
        for t in ("完成", "确定", "应用"):
            b = page.get_by_text(t, exact=True).first
            if b.count() and b.is_visible():
                b.click()
                break
        rand_sleep(0.6, 1.0)
        try:
            body = page.locator("body").inner_text(timeout=3000) or ""
        except Exception:
            body = ""
        if "封面应用成功" in body or "应用成功" in body:
            log.info("已上传自定义封面（1280x960，已回读）")
            shot(page, "ks_cover_uploaded")
        else:
            log.warning("快手封面已写文件，但页面未回读到「应用成功」")
            shot(page, "ks_cover_uploaded")
    except Exception as e:
        log.warning("封面上传失败（不影响发布）：%s", e)
        shot(page, "ks_cover_fail")


def _click_publish(page: Page, title: str = "") -> None:
    # 点发布前先关掉话题联想浮层（#标签 注入后快手会弹「推荐话题」浮层，可能挡住底部发布按钮）
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    page.wait_for_timeout(300)
    # 点一下描述框外的空白处收起联想
    try:
        page.mouse.click(100, 100)
    except Exception:
        pass
    page.wait_for_timeout(400)
    # 再清一次引导浮层
    dismiss_dialogs(page)
    _remove_joyride(page)
    page.wait_for_timeout(500)

    def _do_click() -> bool:
        clicked = page.evaluate("""() => {
            if (!document.getElementById('__ks_joyride_killer')) {
                const s = document.createElement('style');
                s.id = '__ks_joyride_killer';
                s.textContent = '#react-joyride-portal, .react-joyride__overlay, .react-joyride__spotlight, .ant-tour, [class*="tour"], [class*="guide-step"] { pointer-events: none !important; z-index: -1 !important; display: none !important; }';
                document.head.appendChild(s);
            }
            const els = [...document.querySelectorAll('*')].filter(el => {
                const t = (el.textContent || '').trim();
                const cls = (el.className || '').toString();
                return t === '发布' && (cls.includes('button-primary') || cls.includes('_button-primary'));
            });
            if (!els.length) return false;
            const btn = els[els.length - 1];
            btn.scrollIntoView({block: 'center'});
            btn.click();
            return true;
        }""")
        if not clicked:
            # DOM 兜底
            for loc in (
                page.locator("div[class*='button-primary'], div[class*='_button-primary']").filter(has_text=re.compile(r"^\s*发布\s*$")),
                page.locator("button[class*='button-primary'], button[class*='_button-primary']").filter(has_text=re.compile(r"^\s*发布\s*$")),
                page.get_by_role("button", name=SELECTORS["publish_btn_text"], exact=True),
                page.locator("button", has_text=re.compile(r"^\s*发布\s*$")),
            ):
                try:
                    if loc.count():
                        loc.first.click(force=True)
                        clicked = True
                        break
                except Exception:
                    continue
        return clicked

    if not _do_click():
        shot(page, "ks_publish_btn_fail")
        raise KuaishouError("未找到发布按钮，截图见 logs/（页面可能已改版）")
    log.info("已点击发布按钮")

    # 检查是否有「请在视频上传完成后再点击发布」提示，若有则等待后重试
    for _ in range(30):
        page.wait_for_timeout(1000)
        if page.get_by_text("请在视频上传完成后再点击发布", exact=False).count():
            log.info("视频仍处于后台转码中（提示请在上传完成后再发布），等待 3 秒后重试...")
            for confirm_text in ("确认", "确定", "我知道了"):
                c_btn = page.get_by_role("button", name=confirm_text, exact=False).first
                if c_btn.count() and c_btn.is_visible():
                    c_btn.click()
                    break
            page.wait_for_timeout(3000)
            _do_click()
        else:
            break

    # 注意：pc/submit 返回 200 不代表发布成功——账号被风控时接口照常 200 但作品被拦截。
    # 可靠标志：URL 跳转到内容管理页（?status=2 审核中）+ 页面出现「审核中」。
    deadline = time.time() + 60
    jumped = False
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        # 风控/违规拦截提示（仅当已跳转管理页或弹窗出现时检查；「未通过」可能是筛选 tab 文字）
        for text in ("发布失败", "违规", "操作过于频繁", "请稍后再试"):
            if page.get_by_text(text, exact=False).count():
                shot(page, "ks_publish_blocked")
                raise KuaishouError(
                    f"快手发布被拦截（页面提示「{text}」）。"
                    f"发布浏览器直连国内网络（无代理）；若创作者中心显示账号健康，"
                    f"可能是 IP 风控或页面改版，查看截图确认"
                )
        for text in SELECTORS["success_texts"]:
            if page.get_by_text(text, exact=False).count():
                log.info("检测到快手发布成功文字：%s", text)
                return
        if "/article/manage" in page.url:
            log.info("已跳转至作品管理页：%s", page.url)
            jumped = True
            return
    # 未检测到跳转：打开内容管理页核实最新作品是否出现（必须核实标题）
    if not jumped:
        try:
            page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            clean_title = re.sub(r"[【】《》#\s]", "", title)[:10]
            body = page.evaluate("() => document.body.innerText")
            if clean_title and clean_title in body:
                log.info("内容管理页已核实出现新作品「%s」（发布成功）", clean_title)
                return
        except Exception:
            pass
    shot(page, "ks_publish_result_unknown")
    raise KuaishouError("点击发布后未检测到成功标志（管理页未见新作品），请查看 logs/ 截图确认")


def _fetch_ks_url(context) -> str | None:
    """发布成功后到内容管理页抓最新作品链接（尽力而为）。"""
    try:
        page = context.new_page()
        page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(4000)
        link = page.locator("a[href*='short-video']").first
        if link.count():
            href = link.get_attribute("href") or ""
            if href.startswith("http"):
                return href
            if href:
                return "https://www.kuaishou.com" + href
    except Exception as e:
        log.debug("获取作品链接失败：%s", e)
    return None

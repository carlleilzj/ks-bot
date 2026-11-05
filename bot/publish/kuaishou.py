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

from ..config import DATA_DIR, KS_STATE_PATH, LOGS_DIR
from .base import (
    LoginExpired,
    PublishError,
    _remove_joyride,
    dismiss_dialogs,
    fill_editor,
    goto_with_retry,
    launch_chromium,
    new_context,
    persist_state_if_changed,
    rand_sleep,
    reload_with_retry,
    settle,
    shot,
)

log = logging.getLogger(__name__)

PUBLISH_URL = "https://cp.kuaishou.com/article/publish/video"
MANAGE_URL = "https://cp.kuaishou.com/article/manage/video"
COMMENT_URL = "https://cp.kuaishou.com/article/comment"
SPARK_RR_PATH = DATA_DIR / "ks_spark_rr.json"
SPARK_TYPE_LABEL = "关联变现任务"
# 发布页「作者服务」区两个下拉的占位符（2026-09-16 实机核对）
SPARK_TYPE_PLACEHOLDER = "选择服务类型"
# 注意：平台文案改过。旧版是「关联变现任务获得更多收入」，
# 2026-09 起实际页面显示「关联成功可获得更多收益」。两版都带上以兼容。
SPARK_TASK_PLACEHOLDERS = ("关联成功可获得更多收益", "关联变现任务获得更多收入")

# 「作者服务」各服务类型的二级下拉占位符（2026-11-02 实机核对 A 号万粉页面）。
# 星火下架后主力改挂：关联推广任务（星任务/短剧 CPS）+ 关联商品（日用百货 CPS）。
SERVICE_TASK_PLACEHOLDERS = {
    "关联变现任务": SPARK_TASK_PLACEHOLDERS,
    "关联推广任务": ("关联推广任务获得更多收入",),
    "关联商品": ("关联商品获得更多收入",),
}

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
    # 只放强成功信号。「审核中」已移除：发布页本身就可能出现该字样，
    # 一度导致「还停在表单页」被误判成已发布（2026-11-04 修正）。
    "success_texts": ["发布成功", "提交成功"],
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


def _read_desc_text(page: Page) -> str:
    """读取快手当前描述编辑器内容（支持 contenteditable 及 textarea）。"""
    try:
        return page.evaluate("""() => {
            for (const sel of ['#work-description-edit', 'div[contenteditable="true"]', 'textarea']) {
                const el = document.querySelector(sel);
                if (el) {
                    const t = (el.innerText || el.textContent || el.value || '').trim();
                    if (t) return t;
                }
            }
            return '';
        }""") or ""
    except Exception:
        return ""


def _assert_desc_not_empty(page: Page, expected_text: str) -> None:
    """提交前最后防线：校验描述编辑器非空，防止因 React 重置或遮罩拦截发出空标题废片（借鉴 MatrixMedia #34）。"""
    clean_expected = re.sub(r"\s+", "", expected_text or "")
    if not clean_expected:
        return
    current = _read_desc_text(page)
    clean_current = re.sub(r"\s+", "", current)
    if clean_current:
        return
    log.warning("提交前检测到快手描述编辑器为空！尝试再次注入补救...")
    if _fill_desc_js(page, expected_text):
        current = _read_desc_text(page)
        if re.sub(r"\s+", "", current):
            log.info("描述补救注入成功")
            return
    shot(page, "ks_desc_empty_abort")
    raise KuaishouError("快手提交前校验失败：描述编辑器为空（可能被页面重置），为避免发出空标题/空描述废片已中止本次发布")


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
    2026-10-03 修正：session 过期时发布页 URL 不变、内容变成营销落地页
    （只有「立即登录」按钮），旧 URL 前缀判据会误判已登录 →
    登录脚本秒退、发布流程等 file_input 超时。加落地页排除判据。
    """
    try:
        if "passport.kuaishou.com" in page.url:
            return False
        url = page.url
        if url.startswith(("https://cp.kuaishou.com/article/manage",
                           "https://cp.kuaishou.com/article/publish")):
            # 营销落地页排除：session 失效时该 URL 渲染的是平台介绍页，
            # 正文带「立即登录」且无 file_input。二者必须同时排除。
            if page.locator(SELECTORS["file_input"]).count() > 0:
                return True
            try:
                body = page.locator("body").inner_text(timeout=5_000)
            except Exception:
                body = ""
            if "立即登录" in body[:600]:
                return False
            # 管理页正文里有「作品管理」导航
            if "作品管理" in body:
                return True
            return False
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


def _has_login_cookies(context, state_path: Path | str | None = None) -> bool:
    """按 cookie 判定登录态（不依赖页面结构，扫码成功即生效）。

    state_path：域匹配查不到时兜底读的登录态文件（多号必须传对，
    否则会拿 A 号的文件误判 B 号「已登录」——2026-10-24 实测踩过：
    全新 context 被判已登录，二维码根本不出，还把游客态写进了 ks2_state）。
    """
    try:
        cookies = context.cookies(["https://cp.kuaishou.com", "https://www.kuaishou.com",
                                    "https://id.kuaishou.com", "https://passport.kuaishou.com"])
        names = {c["name"] for c in cookies}
        if "userId" in names or "passToken" in names:
            return True
    except Exception:
        pass
    # 域匹配查不到时兜底：直接读该号的 storage_state 文件
    return _cookie_file_has_login(Path(state_path) if state_path else KS_STATE_PATH)


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
            if _has_login_cookies(context, state_path):
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

    产物：logs/kuaishou_qr.png（可直接发到手机扫）。
    矩阵号按登录态文件名派生（ks2_state → kuaishou2_qr.png），互不覆盖。
    """
    if out_path is None:
        stem = Path(state_path).stem
        name = "kuaishou" if stem == "ks_state" else stem
        out_path = LOGS_DIR / f"{name}_qr.png"
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
            if _has_login_cookies(context, state_path) or _is_logged_in(page):
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
                    f"用快手 App 扫码登录创作者中心（登录态 → {Path(state_path).name}）")
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
                if _has_login_cookies(context, state_path):
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
    spark_sink: list | None = None,
    attach_service: str = "",
    attach_prefer: str = "",
    attach_fallback: str = "",
    cover_title: bool = True,
    first_comment: str = "",
    pin_comment: bool = True,
    anchors: dict | None = None,
) -> str | None:
    """上传并发布一条视频，成功返回快手作品链接（获取不到时 None）。

    spark_sink：传入空 list 时，会把本条实际挂上的星火任务名 append 进去，
    供调用方落库做收益归因（星火收益按任务维度结算，见 db.spark_task_stats）。
    """
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
            if not (_is_logged_in(page) or _has_login_cookies(context, state_path)):
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

            # 5. 封面（缺失/过暗先现场抽帧，再与平台「智能推荐封面」对比择优）
            #    大字报（2026-10-29 增长项②）：快手双列发现页先看封面再点，
            #    1280×960 裁切版中央叠 ≤2 行标题钩子。
            #
            #    2026-11-05：
            #    - 条件从 `if cover and cover.exists()` 改为无条件调用。原来的写法
            #      在「封面下载失败 / 文件缺失」时整段跳过上传，平台改用视频首帧
            #      —— 暗色素材首帧往往是全黑的，这是封面全黑的直接原因。
            #    - 新增平台「智能推荐封面」对比择优：转码后快手会给出 3 张高光帧，
            #      比自选封面更亮就用它（代价：推荐封面无法叠标题大字）。
            rescue_cover = rescue_dark_cover(
                Path(cover) if cover else None, Path(video))
            chosen = _pick_best_cover(
                page, rescue_cover,
                title=title if cover_title else "")
            log.info("封面最终来源：%s", chosen)

            # 5.5 挂「作者服务」推广（2026-11-02：星火已下架，改挂关联推广任务）
            #     attach_service 优先：任务池发布页实时读，失败软降级（不阻断）；
            #     attach_fallback：主挂池空时兜底（如关联商品日用品 CPS）。
            #     spark_task 保留原语义：星火挂不上硬阻断（防静默无收益白发）。
            #     挂上的条目名写进 spark_sink，供协调端落库做收益归因。
            attached: str | None = None
            if attach_service:
                attached = _attach_service(page, attach_service, attach_prefer,
                                           state_path=state_path)
                if not attached and attach_fallback:
                    log.info("主挂 %s 未成功，改挂兜底 %s", attach_service, attach_fallback)
                    attached = _attach_service(page, attach_fallback,
                                               state_path=state_path)
                if not attached:
                    # 软降级：平台改版/池空时照常发布。连续出现请查
                    # ks_service_*.png 截图与日志，重新核对下拉占位符。
                    log.warning("⚠️ 推广挂载未生效（按无挂载发布）：%s", attach_service)
                    shot(page, "ks_service_soft_fail")
            elif spark_task:
                attached = _attach_spark_task(page, prefer=spark_task_title,
                                              state_path=state_path)
                if not attached:
                    shot(page, "ks_spark_blocked")
                    raise SparkAttachError(
                        "星火变现任务挂载未生效，已阻断发布（避免无收益白发）。"
                        "请检查：快手 App 星火计划里任务是否仍可挂、额度是否已满、"
                        "或把 config.yaml 的 spark_task 设为 false 以跳过")
            if attached and spark_sink is not None:
                spark_sink.append(attached)

            # 5.6 其余挂载：热点 / 地点 / 作者声明（config.yaml platforms.*.anchors）
            #     2026-11-04 修正：kuaishou_anchor 里这套实现早就写好了，但一直没被
            #     任何地方 import —— 配置里的「热点: 治愈 / 地点: 郴州 / 声明: 内容由AI生成」
            #     实际一条都没挂上。现在接通，全部软降级（挂不上只 warning）。
            anchor_done: list[str] = []
            if anchors:
                try:
                    from .kuaishou_anchor import apply_anchors
                    anchor_done = apply_anchors(page, anchors)
                    if anchor_done:
                        log.info("已挂载：%s", "、".join(anchor_done))
                    else:
                        log.warning("anchors 配置未生效（%s），按无挂载发布", list(anchors))
                        shot(page, "ks_anchors_soft_fail")
                except Exception as e:
                    log.warning("anchors 挂载异常（不影响发布）：%s", str(e)[:150])

            # 5.9 提交前最后防线：断言描述内容非空（防静默发出空标题废片）
            _assert_desc_not_empty(page, desc_full)

            # 6. 点击发布
            _click_publish(page, title=title)

            # 6.1 先获取视频链接（等待新作品在管理页同步落库，给快手后台留足异步时间）
            ks_url = _fetch_ks_url(context, title=title)

            # 6.5 发表作者首评并置顶（神评互动引导，新作品落库后进行，严格防错位）
            if first_comment:
                try:
                    post_first_comment(page, first_comment=first_comment, title=title, pin=pin_comment)
                except Exception as e:
                    log.warning("发表作者首评异常（不影响视频发布）: %s", e)

            shot(page, "ks_publish_done")
            log.info("发布成功：%s", ks_url or "（作品链接获取失败，见创作者中心-内容管理）")
            return ks_url
        finally:
            # 回写登录态：只在 cookie 真变化时才落盘（见 base.persist_state_if_changed）。
            # 实测 sessionid 不轮换，这不是续期机制，只是留存最新值。
            persist_state_if_changed(context, state_path,
                                     domains=["kuaishou.com"])
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


# 星火任务的单条收益权重（元/条），由 monitor.spark_income 归因得出。
# 实测数据（2026-10-15，用账单真实结算金额 ÷ 发布量）：
#   狐缘山间 0.285 元/条（厂商已下架，不可挂）
#   扫墓丫头，替祖宗横扫天下 0.078 元/条
#   山猎诡事：1977 0.003 元/条（已停）
#   千万际遇辨亲情 0.002 元/条（已停）
# 权重表用于把「均匀轮换」改成「按收益加权」——同样的播放量，
# 把曝光集中到高产出任务上。任务下架后会自动跳过（候选列表里没有）。
SPARK_TASK_WEIGHTS = {
    "狐缘山间": 0.285,
    "扫墓丫头，替祖宗横扫天下": 0.078,
    "我花五万买破船，里面藏着万吨黄金": 0.05,   # 样本少，暂给中位估值
    "榆树沟出了个辣媳妇1": 0.06,               # 2026-09-10 新上架，同题材估值
}

# 明确停掉/无产出的任务（不挂）：挂着它们只是浪费曝光位。
SPARK_TASK_BLOCKLIST = (
    "山猎诡事",      # 已停
    "千万际遇辨亲情",  # 已停
)


def _spark_weight(title: str) -> float:
    """任务标题 → 收益权重（元/条）。未知任务给保守默认值。

    标题里带「任务时间：...」后缀，用子串匹配。
    """
    for key, w in SPARK_TASK_WEIGHTS.items():
        if key in title:
            return w
    return 0.02  # 未知任务：保守估值，低于已知最优但不为 0


def pick_spark_title(titles: list[str], prefer: str = "", cursor: int = 0) -> tuple[str, int]:
    """从收藏任务里选一个。

    优先级：
      1. prefer 子串命中 → 直接选（人工指定）
      2. 按收益权重加权轮询（不是均匀轮询）——高产出任务拿更多曝光
    黑名单任务（已停/无产出）直接剔除，不参与轮换。
    返回 (标题, 下一个 cursor)。
    """
    clean = [t.strip() for t in titles if t and t.strip()]
    # 剔除黑名单
    clean = [t for t in clean
             if not any(b in t for b in SPARK_TASK_BLOCKLIST)]
    if not clean:
        return "", cursor
    if prefer:
        for t in clean:
            if prefer in t:
                return t, cursor
    if len(clean) == 1:
        # cursor 仍要推进：调用方靠它判断"是否换过任务"
        return clean[0], cursor + 1

    # 加权轮询：权重按「最大权重 : 其余」压缩成有上限的整数票数，
    # 保证低权重任务也有固定曝光（不出现被高权重完全挤掉的情况）。
    # 曾用 w/total*1000 直接换算，0.078 vs 0.05 → 609 vs 390 看似合理，
    # 但权重差距大时（0.285 vs 0.002）低权重会缩到 0 票而永不曝光。
    weights = [_spark_weight(t) for t in clean]
    top = max(weights)
    if top <= 0:
        i = cursor % len(clean)
        return clean[i], cursor + 1

    # 每档最多 10 票，最少 1 票 → 高权重拿 10 票、最低也有 1 票
    slots: list[str] = []
    for t, w in zip(clean, weights):
        n = max(1, min(10, round(w / top * 10)))
        slots.extend([t] * n)
    chosen = slots[cursor % len(slots)]
    return chosen, cursor + 1


def _spark_sidecars(state_path: Path | str | None) -> tuple[Path, Path]:
    """按登录态文件派生（星火轮换 cursor, 任务池缓存）两个伴生文件。

    A 号（ks_state.json）沿用历史文件名 ks_spark_rr.json / ks_spark_pool.json，
    双号改造不迁移不重置；其他号（如 ks2_state.json）按 stem 推导。
    """
    name = Path(state_path or KS_STATE_PATH).name
    if name == "ks_state.json":
        return DATA_DIR / "ks_spark_rr.json", DATA_DIR / "ks_spark_pool.json"
    stem = Path(name).stem
    return DATA_DIR / f"{stem}_rr.json", DATA_DIR / f"{stem}_pool.json"


def _spark_rr_load(rr_path: Path | None = None) -> int:
    p = rr_path or _spark_sidecars(None)[0]
    try:
        return int(json.loads(p.read_text(encoding="utf-8")).get("i", 0) or 0)
    except Exception:
        return 0


def _spark_rr_save(cursor: int, rr_path: Path | None = None) -> None:
    p = rr_path or _spark_sidecars(None)[0]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"i": cursor}), encoding="utf-8")
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


# ---------- 作者服务通用挂载（2026-11-02，星火替代路线） ----------

def _pick_service_item(items: list[str], prefer: str = "", cursor: int = 0) -> tuple[str, int]:
    """从二级下拉条目里选一个：prefer 命中的条目内轮换；无命中则全量轮换。

    短剧任务常同时有 2 条（030 期/031 期），轮换能让两条都吃到曝光，
    也便于按任务维度做收益归因。
    """
    clean = [t.strip() for t in items if t and t.strip()]
    if not clean:
        return "", cursor
    if prefer:
        hits = [t for t in clean if prefer in t]
        if hits:
            return hits[cursor % len(hits)], cursor + 1
    return clean[cursor % len(clean)], cursor + 1


def _open_service_dropdown(page: Page) -> list[str]:
    """展开「作者服务」类型下拉并读选项。

    首次展开时占位符是「选择服务类型」；若本轮已选过一次（主挂失败改兜底），
    占位符消失，此时直接展开第一个 .ant-select。
    """
    opts = _open_select_dropdown(page, SPARK_TYPE_PLACEHOLDER)
    if opts:
        return opts
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        sel = page.locator(".ant-select").nth(0)
        if sel.count():
            sel.click()
            page.wait_for_timeout(900)
            return _dropdown_option_texts(page)
    except Exception:
        pass
    return []


def _service_attached(page: Page, service_label: str, chosen: str) -> bool:
    """回读表单：服务类型栏 == service_label 且任务栏出现所选条目（前缀匹配）。

    复用 _spark_form_state 的 picks（所有 antd 选中项文本）——它对服务类型
    的归类假设是星火，但 picks 本身是通用的。
    """
    picks = [_spark_title_probe(page, p) for p in (_spark_form_state(page).get("picks") or [])]
    picks = [p for p in picks if p]
    if service_label not in picks:
        return False
    key = _spark_title_probe(page, chosen)
    if not key:
        return False
    return any(
        p != service_label and (p == key or key[:6] in p or p[:6] in key)
        for p in picks
    )


def _attach_service(page: Page, service_label: str, prefer: str = "",
                    state_path: Path | str | None = None) -> str | None:
    """挂「作者服务」下指定类型的推广条目（关联推广任务/关联商品/星火）。

    成功返回挂上的条目文本（平台可能带「任务时间：…」/价格后缀），
    失败一律返回 None —— 由调用方决定硬阻断（星火）还是软降级（推广/商品）。
    """
    try:
        dismiss_dialogs(page)
        _remove_joyride(page)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(400)

        # 第一层：服务类型
        opts = _open_service_dropdown(page)
        if not opts:
            log.warning("挂 %s 失败：作者服务下拉打不开/为空", service_label)
            shot(page, "ks_service_no_entry")
            return None
        if service_label not in opts:
            log.warning("挂 %s 失败：下拉里没有该类型，实际=%s", service_label, opts)
            shot(page, "ks_service_no_type")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return None
        if not _click_visible_option(page, service_label):
            log.warning("挂 %s 失败：点不开该类型", service_label)
            shot(page, "ks_service_type_click_fail")
            return None
        page.wait_for_timeout(1200)

        # 第二层：任务/商品下拉（占位符按类型）
        titles: list[str] = []
        matched_ph = ""
        for ph in SERVICE_TASK_PLACEHOLDERS.get(service_label, ()):
            titles = _open_select_dropdown(page, ph)
            if titles:
                matched_ph = ph
                break
        if not titles:
            log.warning("挂 %s 失败：二级下拉打不开或列表为空（占位符 %s）",
                        service_label, list(SERVICE_TASK_PLACEHOLDERS.get(service_label, ())))
            shot(page, "ks_service_empty_list")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return None
        log.info("%s 下拉已展开（%s）：%d 个候选 %s",
                 service_label, matched_ph, len(titles), titles[:4])

        rr_path = _spark_sidecars(state_path)[0]
        chosen, nxt = _pick_service_item(titles, prefer=prefer,
                                         cursor=_spark_rr_load(rr_path))
        if not chosen:
            log.warning("挂 %s 失败：未能选出条目", service_label)
            shot(page, "ks_service_pick_fail")
            return None
        if not _select_spark_option(page, chosen):
            log.warning("挂 %s 失败：条目「%s」点不到", service_label, chosen)
            shot(page, "ks_service_option_click_fail")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return None

        # 回读验证：交互成功 ≠ 平台已接受（2026-09-13 起的老教训）
        page.wait_for_timeout(900)
        if not _service_attached(page, service_label, chosen):
            log.warning("挂 %s 未生效：点选「%s」后表单回读未确认", service_label, chosen)
            shot(page, "ks_service_verify_fail")
            return None

        _spark_rr_save(nxt, rr_path)
        page.wait_for_timeout(400)
        log.info("已挂 %s（回读校验通过）：%s", service_label, chosen)
        return chosen
    except Exception as e:
        log.warning("挂 %s 异常：%s", service_label, e)
        shot(page, "ks_service_attach_fail")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return None


def _attach_spark_task(page: Page, prefer: str = "",
                       state_path: Path | str | None = None) -> str | None:
    """发布表单挂星火「关联变现任务」。

    state_path 决定轮换 cursor / 任务池缓存写到哪个文件（多号隔离，
    见 _spark_sidecars）；不传 = A 号默认路径。
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

        # 任务池变化跟踪：新任务上架/老任务消失时推 TG（收藏只能在 App 做，
        # 人工盯不过来，见 monitor/spark_pool）。失败不影响发布。
        try:
            from ..monitor.spark_pool import note_candidates
            from ..notify import telegram as _tg

            def _notify(text: str) -> None:
                from ..config import load_settings
                _tg.send_text(load_settings(), text)

            note_candidates(titles, notify=_notify,
                            pool_path=_spark_sidecars(state_path)[1])
        except Exception as e:
            log.debug("任务池变化检测失败（不影响发布）：%s", e)

        rr_path = _spark_sidecars(state_path)[0]
        chosen, nxt = pick_spark_title(titles, prefer=prefer, cursor=_spark_rr_load(rr_path))
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

        _spark_rr_save(nxt, rr_path)
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


# 封面过暗阈值：低于此值发布前现场换帧。
# 2026-11-05 从 26 提到 42：实测封面上线后大面积全黑，但线上作品 luma=40.6
# 被旧阈值判为「合格」直接发布 —— 26 只挡得住纯黑帧，挡不住「暗到看不清主体」。
# 快手双列发现页是缩略图，人眼可辨识的下限远高于纯黑判据。
DARK_COVER_LUMA = 42.0


def rescue_dark_cover(cover: Path | None, video: Path) -> Path | None:
    """发布端兜底：封面缺失/过暗 → 从视频现抽代表帧。

    协调端选优已过滤暗帧，这里是第二道防线（覆盖：旧任务缓存封面、
    选优降级、上传文件损坏、封面下载失败等漏网情形）。
    任何失败都返回原封面，不阻断发布。

    2026-11-05 修正两处漏网（曾导致作品封面一片全黑）：
      1. 原实现 `if cover is None: return None` —— 封面下载失败时直接放行，
         调用方再因 `if cover and cover.exists()` 为假整段跳过封面上传，
         平台改用视频首帧（暗色素材往往是黑的）。
         → 现在封面缺失也从视频抽帧。
      2. 暗帧阈值 26 太低，luma=40 的暗封面照样被判合格。
         → 阈值提到 42（见 DARK_COVER_LUMA）。
    """
    try:
        from ..media.ffmpeg import _ffmpeg, _run, mean_luma  # noqa: SLF001
    except Exception:
        return cover
    try:
        video = Path(video)
        cover = Path(cover) if cover else None

        y = None
        if cover is not None and cover.exists():
            y = mean_luma(cover)
            if y is not None and y >= DARK_COVER_LUMA:
                return cover
            log.warning("封面过暗（luma=%s），发布前从视频重抽代表帧",
                        f"{y:.1f}" if y is not None else "未知")
        elif cover is None:
            log.warning("封面缺失（下载失败或未生成），发布前从视频抽帧兜底")
        else:
            log.warning("封面文件不存在（%s），发布前从视频抽帧兜底", cover)

        if not video.exists():
            log.warning("视频文件不存在，无法抽帧（按原状态发布）")
            return cover

        out = (cover.with_suffix(".rescue.jpg") if cover
               else video.parent / f"{video.stem}_cover_rescue.jpg")
        # thumbnail 滤镜：每 200 帧里选信息量最大的一帧（比中段盲截好）
        _run([_ffmpeg(), "-y", "-i", str(video),
              "-vf", "thumbnail=200", "-frames:v", "1", "-q:v", "2", str(out)],
             timeout=120)
        if out.exists() and out.stat().st_size > 2500:
            y2 = mean_luma(out)
            # 抽出帧只要不比原图更暗就采纳（原图缺失时无条件采纳）
            if y is None or y2 is None or y2 >= DARK_COVER_LUMA or y2 > y:
                if y is not None:
                    log.info("暗封面已替换：luma %.1f → %s", y,
                             f"{y2:.1f}" if y2 is not None else "未知")
                else:
                    log.info("已用视频代表帧补齐封面：luma %s",
                             f"{y2:.1f}" if y2 is not None else "未知")
                return out
            log.warning("代表帧仍暗（luma=%s），按原封面发布",
                        f"{y2:.1f}" if y2 is not None else "未知")
        else:
            log.warning("抽帧失败或产出过小，按原封面发布")
        return cover
    except Exception as e:
        log.debug("暗封面替换失败（按原封面发布）：%s", str(e)[:100])
        return cover


# 快手「智能推荐封面」的选择器（2026-11-05 实机核对）
# 结构：封面设置区右侧 3 个缩略图槽位，图未就绪时带 _loading_ 类，
# 加载完成后 img.src 才是可下载的 CDN 地址（加载中为空）。
_RECO_ITEM_SEL = 'div[class*="recommend-cover-item"]'
_RECO_STATE_JS = """() => {
  const items = [...document.querySelectorAll('div[class*="recommend-cover-item"]')];
  return items.map((el, i) => {
    const cls = (el.className || '').toString();
    const img = el.querySelector('img');
    return {
      i: i,
      loading: cls.indexOf('loading') >= 0,
      src: img ? (img.getAttribute('src') || '') : ''
    };
  });
}"""


def _wait_recommend_covers(page: Page, timeout_sec: float = 45.0) -> list[str]:
    """等平台「智能推荐封面」加载完，返回可下载的 CDN 地址列表。

    只在视频转码完成后才会出现；图未就绪时 div 带 _loading_ 类、img.src 为空。
    超时或没有推荐封面返回空列表（调用方按原有自选封面走）。
    """
    import time as _time

    deadline = _time.time() + timeout_sec
    while _time.time() < deadline:
        try:
            state = page.evaluate(_RECO_STATE_JS) or []
        except Exception:
            state = []
        if state and not any(x.get("loading") for x in state):
            urls = [x["src"] for x in state if (x.get("src") or "").startswith("http")]
            if urls:
                log.info("平台推荐封面已就绪：%d 张", len(urls))
                return urls
        page.wait_for_timeout(2000)
    log.debug("平台推荐封面未在 %.0fs 内就绪（按自选封面走）", timeout_sec)
    return []


def _download_cover_urls(urls: list[str], out_dir: Path) -> list[Path]:
    """下载推荐封面到本地，返回成功落盘的文件列表。"""
    import httpx

    out_dir.mkdir(parents=True, exist_ok=True)
    got: list[Path] = []
    for i, url in enumerate(urls):
        try:
            with httpx.Client(timeout=25, follow_redirects=True) as c:
                r = c.get(url, headers={"User-Agent": "Mozilla/5.0",
                                        "Referer": "https://cp.kuaishou.com/"})
            if r.status_code != 200 or len(r.content) < 3000:
                continue
            f = out_dir / f"reco_{i}.jpg"
            f.write_bytes(r.content)
            got.append(f)
        except Exception as e:
            log.debug("推荐封面下载失败[%d]：%s", i, str(e)[:80])
    return got


def _apply_recommend_cover(page: Page, index: int) -> bool:
    """点击第 index 个平台推荐封面，并回读主封面是否真的切换。"""
    try:
        loc = page.locator(_RECO_ITEM_SEL)
        if loc.count() <= index:
            return False
        before = page.evaluate("""() => {
            const el = document.querySelector('[class*="_default-cover"]');
            const img = el ? el.querySelector('img') : null;
            return img ? (img.getAttribute('src') || '') : '';
        }""")
        loc.nth(index).click(timeout=15000)
        page.wait_for_timeout(2500)
        after = page.evaluate("""() => {
            const el = document.querySelector('[class*="_default-cover"]');
            const img = el ? el.querySelector('img') : null;
            return img ? (img.getAttribute('src') || '') : '';
        }""")
        ok = bool(after) and after != before
        if ok:
            log.info("已采用平台推荐封面（第 %d 张）", index + 1)
        else:
            log.warning("点击推荐封面后主封面未变化（第 %d 张）", index + 1)
        return ok
    except Exception as e:
        log.debug("采用推荐封面失败：%s", str(e)[:100])
        return False


def _pick_best_cover(
    page: Page, self_cover: Path | None, title: str = "",
) -> str:
    """封面择优：自选封面 vs 平台推荐封面，取更亮的那个。

    背景（2026-11-05）：快手发布页在视频转码后会给出 3 张「智能推荐封面」，
    它们取自视频高光帧，通常比我们按亮度/评分选出的帧更贴合平台推荐流。
    但并非总是更好（可能带黑边、主体不清），所以做**对比择优**而不是无脑替换。

    策略：
      1. 先等推荐封面就绪并下载，测亮度；
      2. 自选封面（已过 rescue 兜底）也测亮度；
      3. 推荐封面里取最亮的一张，若比自选封面更亮 → 先上传自选封面（保留
         标题大字能力），再点平台推荐覆盖它；否则保持自选封面。
         注意：推荐封面是平台生成的原图，无法叠标题大字——这是选择它的代价。

    返回 "recommend" / "self" / "none"，仅用于日志与测试断言。
    """
    from ..media.ffmpeg import mean_luma

    urls = _wait_recommend_covers(page)
    reco_files: list[Path] = []
    if urls:
        base = (self_cover.parent if self_cover else Path("/tmp"))
        reco_files = _download_cover_urls(urls, base / "reco_cover")

    self_y: float | None = None
    if self_cover and Path(self_cover).exists():
        self_y = mean_luma(Path(self_cover))

    best_reco: tuple[int, float] | None = None
    for i, f in enumerate(reco_files):
        y = mean_luma(f)
        if y is None:
            continue
        if best_reco is None or y > best_reco[1]:
            best_reco = (i, y)

    if self_cover and Path(self_cover).exists():
        _upload_cover(page, Path(self_cover), title=title)
        log.info("已上传自选封面（luma=%s）",
                 f"{self_y:.1f}" if self_y is not None else "未知")

    if best_reco is None:
        if self_cover and Path(self_cover).exists():
            return "self"
        log.warning("既无自选封面也无平台推荐封面，平台将改用视频帧")
        return "none"

    idx, reco_y = best_reco
    log.info("平台推荐封面最亮 luma=%.1f（自选封面 luma=%s）", reco_y,
             f"{self_y:.1f}" if self_y is not None else "无")
    if self_y is None or reco_y > self_y:
        if _apply_recommend_cover(page, idx):
            return "recommend"
    return "self" if (self_cover and Path(self_cover).exists()) else "none"


def _upload_cover(page: Page, cover: Path, title: str = "") -> None:
    """上传选优封面的 4:3 / 1280×960 裁切版（title 非空时叠标题大字）。

    快手官方提示不低于 1280×960；直接塞 720×1280 的 9:16 会弹「应用成功」
    但信息流仍用视频帧。必须写到 file input，并用页面文案回读。

    大字报在裁切**之后**叠：文字按最终 1280×960 版面取宽（居中放置，
    裁切不裁中心，但先裁后叠的字号才是用户实际看到的字号）。
    """
    from ..media.ffmpeg import prepare_platform_covers

    try:
        variants = prepare_platform_covers(cover)
        img = variants.get("landscape") or cover
        titled = False
        if title:
            # 延迟导入 + 内层兜底：cover_text 依赖 Pillow，发布机 venv
            # 历史上从不需要 PIL；万一缺库，降级为无字封面继续上传，
            # 而不是让整个封面步骤失败（平台改用视频帧，损失更大）。
            try:
                from ..media.cover_text import overlay_title_text
                img = overlay_title_text(img, title)
                titled = img.name.endswith("_titled.jpg")
            except Exception as e:
                log.warning("封面大字叠字失败（按无字封面发布）：%s", str(e)[:120])

        slot_src_before = ""
        try:
            slot_src_before = page.evaluate("""() => {
                const img = document.querySelector('[class*="_default-cover"] img, [class*="cover"] img');
                return img ? img.src : '';
            }""") or ""
        except Exception:
            pass

        img_input = page.locator(SELECTORS["cover_input"]).first
        opened = img_input.count() > 0
        if not opened:
            # 尝试通过按钮文案或类名定位封面编辑器入口（借鉴 MatrixMedia 探针经验）
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

            if not opened:
                # 尝试类名选择器 [class*="_cover-full-editor"] / [class*="_default-cover"]
                opened = bool(page.evaluate("""() => {
                    const el = document.querySelector('[class*="_cover-full-editor"], [class*="_default-cover"]');
                    if (el) { el.click(); return true; }
                    return false;
                }"""))
                if opened:
                    rand_sleep(1.0, 1.5)
                    up = page.get_by_text("上传封面", exact=False).first
                    if up.count() and up.is_visible():
                        up.click()
                        rand_sleep()
                    img_input = page.locator(SELECTORS["cover_input"]).first

        if not opened or not img_input.count():
            log.info("未找到封面上传入口，平台将用视频帧")
            shot(page, "ks_cover_no_input")
            return
        img_input.set_input_files(str(img))
        rand_sleep(1.2, 2.0)

        # 裁剪比例锁定 3:4（借鉴 MatrixMedia 探针实证配方，3:4 适配快手双列发现流）
        try:
            ratio_res = page.evaluate("""() => {
                const norm = (t) => String(t || '').replace(/\\s+/g, '').trim();
                const m = document.querySelector('.ant-modal');
                if (!m) return false;
                const cands = [];
                for (const el of m.querySelectorAll('[class*="ratio"] *')) {
                    const t = norm(el.textContent);
                    if ((t === '3:4' || t === '3:4推荐') && el.children.length <= 2) {
                        cands.push(el);
                    }
                }
                if (!cands.length) return false;
                cands.sort((a, b) => a.children.length - b.children.length);
                cands[0].click();
                return true;
            }""")
            if ratio_res:
                log.info("已选定快手封面裁剪比例 3:4")
        except Exception as e:
            log.debug("3:4 裁剪比例选择异常（忽略）：%s", e)

        for t in ("完成", "确定", "应用", "确认"):
            b = page.get_by_text(t, exact=True).first
            if b.count() and b.is_visible():
                b.click()
                break
        rand_sleep(0.8, 1.5)

        # 回读与校验：1. 检查 CDN / src 变化（MatrixMedia 强信号）；2. 检查页面提示文案
        after_src = ""
        try:
            after_src = page.evaluate("""() => {
                const img = document.querySelector('[class*="_default-cover"] img, [class*="cover"] img');
                return img ? img.src : '';
            }""") or ""
        except Exception:
            pass
        cdn_ok = bool(re.search(r"ssrcdn|kwaicdn|kwcdn|kscdn|yximgs", after_src))
        changed_ok = bool(after_src and after_src != slot_src_before)

        try:
            body = page.locator("body").inner_text(timeout=3000) or ""
        except Exception:
            body = ""
        text_ok = "封面应用成功" in body or "应用成功" in body

        if cdn_ok or changed_ok or text_ok:
            log.info("已上传自定义封面（1280x960，已回读%s，CDN=%s）",
                     "，叠标题大字" if titled else "", cdn_ok)
            shot(page, "ks_cover_uploaded")
        else:
            log.warning("快手封面已写文件，但页面未回读到「应用成功」或 CDN 变更")
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
    # 可靠标志：URL 跳转到内容管理页，或内容管理页确实出现了这条标题。
    #
    # 2026-11-04 修正三处误判：
    #  1) 「违规」不能全页模糊匹配 —— 创作者中心每页都有「请勿发布违规内容」
    #     「违规记录」等常驻文案，一旦命中就在视频其实已发出后判失败 → 重试 → 重复发布。
    #     改为只看可见的 toast/弹窗容器，且只认精确的风控短语。
    #  2) 「审核中」从成功标志里移除（发布页本身就有该字样 → 假阳性）。
    #  3) 即使看到「发布成功」字样，也要求已经离开发布表单页才认 ——
    #     上传页自身文案里的成功字样不能当作提交成功。
    #  4) 标题回查在作品卡片内逐块核对，不再拿整页 body 做 in。
    BLOCK_TOASTS = ("操作过于频繁", "发布失败，请稍后再试", "发布失败")
    saw_success = False   # 见过成功 toast 就记住，不因它消失而丢失信号
    deadline = time.time() + 60
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        if "/article/manage" in page.url:
            log.info("已跳转至作品管理页：%s", page.url)
            return
        left_form = PUBLISH_URL not in page.url
        toast_text = _visible_toast_text(page)
        if toast_text:
            for text in BLOCK_TOASTS:
                if text in toast_text:
                    shot(page, "ks_publish_blocked")
                    raise KuaishouError(
                        f"快手发布被拦截（弹窗提示「{text}」）。"
                        f"发布浏览器直连国内网络（无代理）；若创作者中心显示账号健康，"
                        f"可能是 IP 风控或页面改版，查看截图确认"
                    )
            for text in SELECTORS["success_texts"]:
                if text in toast_text:
                    # toast 是瞬时的：可能出现在跳转之前就消失。
                    # 记住「看见过」，等真离开发布页再认成功，避免同一轮里
                    # 既错过 toast、又因页面未跳转而误判失败 → 重复发布。
                    saw_success = True
                    if left_form:
                        log.info("检测到快手发布成功提示且已离开发布页：%s", text)
                        return
                    log.debug("看到「%s」但仍停在发布页，继续等待跳转", text)
    if saw_success:
        # 见过成功提示，只是没赶上 URL 跳转：再给一次核实机会
        try:
            page.wait_for_timeout(3000)
            if PUBLISH_URL not in page.url or _title_in_manage_list(page, title):
                log.info("曾检测到发布成功提示，且已离开发布页/管理页可见新作品 → 判定发布成功")
                return
        except Exception:
            pass
    # 未检测到跳转：打开内容管理页核实最新作品是否出现（必须核实标题）
    try:
        page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)
        if _title_in_manage_list(page, title):
            log.info("内容管理页已核实出现新作品「%s」（发布成功）", title[:20])
            return
    except Exception:
        pass
    shot(page, "ks_publish_result_unknown")
    raise KuaishouError("点击发布后未检测到成功标志（管理页未见新作品），请查看 logs/ 截图确认")


_TOAST_SELECTORS = (
    ".ant-message-notice-content", ".cp-message", ".el-message", ".el-message-box__message",
    "[class*='toast']", "[class*='Toast']", "[role='alert']", ".cp-dialog__body",
)


def _visible_toast_text(page: Page) -> str:
    """只取可见的 toast/弹窗文案，避免把页面常驻说明当成发布结果。"""
    parts: list[str] = []
    for sel in _TOAST_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible():
                    parts.append(el.inner_text() or "")
        except Exception:
            continue
    return "\n".join(p for p in parts if p)


def _title_in_manage_list(page: Page, title: str) -> bool:
    """在作品列表卡片内核对标题，绝不拿整页 body 做 `in` 判断。

    同模板标题的前缀常常相同，整页模糊匹配会被历史作品命中 → 把失败记成成功、静默漏发。

    实现要点（2026-11-04 加固）：
    - 不假设卡片是「时长行 → 标题 → 日期」三行：新发布作品可能显示「审核中/转码中」
      而没有时长行，置顶徽标也会多出一行。改为在每块里扫所有行找匹配。
    - 只认**第一块**（最新作品，快手按发布时间倒序）：历史作品/置顶卡片同名前缀
      会让失败误判成成功。_fetch_ks_url 已按同样思路排除置顶。
    """
    clean = re.sub(r"[【】《》#\s\-_，。！？、“”\"']", "", title)
    if len(clean) < 6:
        return False
    try:
        body = page.evaluate("() => document.body.innerText")
    except Exception:
        return False
    target = clean[:10]
    blocks = [b for b in re.split(r"(?m)^(?=\d{1,2}:\d{2}(?::\d{2})?\s*$)", body)
              if b.strip()]
    if not blocks:
        return False
    # 只看最新的 2 块：新作品偶尔会排在「置顶作品」之后
    for blk in blocks[:2]:
        for line in blk.splitlines():
            norm_line = re.sub(r"[【】《》#\s\-_，。！？、“”\"']", "", line.strip())
            if norm_line.startswith(target[:8]):
                return True
    return False


def _fetch_ks_url(context, title: str = "", max_attempts: int = 10) -> str | None:
    """发布成功后到内容管理页抓最新作品链接（尽力而为）。

    关键要求与反脆弱机制（吸取 2026-11-03 实测踩坑教训）：
    1. 绝不盲目取 items[0]：快手置顶作品排在最前且常驻，盲目取第一条会把历史置顶视频误判为刚发布的新片！
    2. 优先按标题多重匹配：提取标题核心（去掉前后缀、标点、话题），双向包含比对；
    3. 多轮轮询与定时刷新：实测新作品从「审核中」进入作品列表需 30~90 秒，
       默认 10 轮 × 6 秒（约 60 秒）+ 每 2 轮 reload，覆盖绝大多数同步延迟；
    4. 兜底保护：若最终仍未命中目标标题，返回 None（由发布端后续补链接任务兜底），
       也坚决不误报置顶视频。
    """
    page = None
    try:
        page = context.new_page()
        page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=30_000)

        for attempt in range(1, max_attempts + 1):
            page.wait_for_timeout(6000)
            try:
                page.wait_for_selector(".video-item", timeout=6000)
                # 滚到底触发懒加载，保证新作品卡片被渲染
                page.evaluate("""() => {
                    window.scrollTo(0, document.documentElement.scrollHeight);
                    document.querySelectorAll('*').forEach(el => {
                        if (el.scrollHeight > el.clientHeight + 100 && el.clientHeight > 200) {
                            el.scrollTop = 0;
                        }
                    });
                }""")
                page.wait_for_timeout(800)
            except Exception:
                pass

            # 策略 1：通过封面图片的 clientCacheKey 提取匹配标题的作品 URL
            try:
                res = page.evaluate("""([name]) => {
                    const norm = (t) => String(t || '').replace(/[\\s\\-_，。！？、“”"''（）《》【】#]/g, '').toLowerCase();
                    const cleanName = norm(name).slice(0, 15);
                    const items = document.querySelectorAll('.video-item');
                    if (!items.length) return null;

                    for (let i = 0; i < items.length; i++) {
                        const item = items[i];
                        const titleEl = item.querySelector('.video-item__detail__row__title, [class*="title"]');
                        const titleText = titleEl ? (titleEl.textContent || '') : '';
                        const cleanTitle = norm(titleText);

                        let matched = false;
                        if (cleanName && cleanTitle) {
                            if (cleanTitle.includes(cleanName) || cleanName.includes(cleanTitle.slice(0, 15))) {
                                matched = true;
                            }
                        }

                        if (matched) {
                            const img = item.querySelector('.video-item__cover img, [class*="cover"] img, img');
                            const src = img ? (img.getAttribute('src') || img.src || '') : '';
                            const m = src.match(/clientCacheKey=([^&]+)/);
                            if (m && m[1]) {
                                const key = m[1].split('.')[0];
                                return {
                                    url: 'https://www.kuaishou.com/short-video/' + key,
                                    title: titleText.split('\\n')[0].trim(),
                                    idx: i
                                };
                            }
                        }
                    }
                    return null;
                }""", [title])

                if res and res.get("url"):
                    log.info("通过 clientCacheKey 精确匹配到快手作品链接 (第 %d 次尝试, idx=%s): %s",
                             attempt, res.get("idx"), res["url"])
                    return res["url"]

            except Exception as e:
                log.debug("第 %d 次尝试通过 clientCacheKey 提取快手链接异常：%s", attempt, e)

            if attempt < max_attempts:
                log.info("快手新作品尚未同步到列表（第 %d/%d 次），刷新管理页重试...", attempt, max_attempts)
                # 每 2 轮才 reload 一次：reload 开销大（2~4s），频繁刷新反而挤占同步窗口
                if attempt % 2 == 0:
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=20_000)
                    except Exception:
                        pass

        # 策略 2：最后一轮谨慎兜底——必须严格排除置顶卡片，检查第 1 个非置顶卡片标题是否部分关联
        try:
            fallback_res = page.evaluate("""([name]) => {
                const norm = (t) => String(t || '').replace(/[\\s\\-_，。！？、“”"''（）《》【】#]/g, '').toLowerCase();
                const cleanName = norm(name).slice(0, 8);
                const items = document.querySelectorAll('.video-item');
                for (let i = 0; i < items.length; i++) {
                    const it = items[i];
                    // 严格排除置顶
                    const hasTopEl = !!it.querySelector('.video-item__detail__row__top');
                    const hasTopText = /\\b置顶\\b|\\s置顶\\s/.test(it.innerText);
                    if (hasTopEl || hasTopText) {
                        continue;
                    }

                    const titleEl = it.querySelector('.video-item__detail__row__title, [class*="title"]');
                    const titleText = titleEl ? (titleEl.textContent || '') : '';
                    const cleanTitle = norm(titleText);

                    // 仅当首个非置顶卡片包含标题前缀时才采纳，否则宁可放弃兜底
                    if (cleanName && cleanTitle && (cleanTitle.includes(cleanName) || cleanName.includes(cleanTitle.slice(0, 8)))) {
                        const img = it.querySelector('.video-item__cover img, [class*="cover"] img, img');
                        const src = img ? (img.getAttribute('src') || img.src || '') : '';
                        const m = src.match(/clientCacheKey=([^&]+)/);
                        if (m && m[1]) {
                            const key = m[1].split('.')[0];
                            return {
                                url: 'https://www.kuaishou.com/short-video/' + key,
                                title: titleText.split('\\n')[0].trim()
                            };
                        }
                    }
                    break;
                }
                return null;
            }""", [title])

            if fallback_res and fallback_res.get("url"):
                log.info("通过非置顶兜底匹配到快手作品链接: %s", fallback_res["url"])
                return fallback_res["url"]
        except Exception as e:
            log.debug("非置顶兜底提取异常：%s", e)

        log.warning("快手内容管理页经 %d 轮轮询未匹配到新作品标题「%s」，放弃返回链接（避免误判置顶视频）",
                    max_attempts, title[:20])

    except Exception as e:
        log.debug("获取作品链接失败：%s", e)
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass
    return None


def fetch_published_url(state_path: Path | str, title: str,
                        max_attempts: int = 4) -> str | None:
    """补链接专用：独立开会话，按标题到作品管理页找已发布作品的公开链接。

    与发布流程内的 _fetch_ks_url 同一套匹配规则（按标题、过滤置顶），
    但轮次更少（发布后补链接通常已在列表里，几轮即可）。
    找不到返回 None，绝不返回不确定结果的链接。
    """
    state_path = Path(state_path)
    if not state_path.exists():
        return None
    with sync_playwright() as p:
        browser = launch_chromium(p, headless=True)
        try:
            context = new_context(browser, state_path)
            try:
                return _fetch_ks_url(context, title=title, max_attempts=max_attempts)
            finally:
                context.close()
        finally:
            try:
                browser.close()
            except Exception:
                pass


def post_first_comment(
    page: Page,
    first_comment: str,
    title: str = "",
    pin: bool = True,
    timeout_sec: float = 25.0,
) -> bool:
    """在快手创作者中心互动管理-评论管理页，发表作者首评并置顶（神评互动引导）。

    失败静默打 warning，绝不阻断发布主流程。
    """
    first_comment = (first_comment or "").strip()
    if not first_comment:
        return False

    # 标题核对必须在打开页面之前就判定：标题太短（或清洗后为空）时无法可靠
    # 区分作品，宁可不发首评，也绝不能落到「当前默认选中的上一条作品」下面。
    clean_target = re.sub(r"[【】《》#\s\-_，。！？、“”\"'']", "", title)[:8].lower()
    if len(clean_target) < 4:
        log.warning("首评目标标题过短或为空（%r），放弃发表首评", title[:20])
        return False

    log.info("准备发表快手作者首评: %s", first_comment)
    try:
        page.goto(COMMENT_URL, wait_until="domcontentloaded", timeout=int(timeout_sec * 1000))
        page.wait_for_timeout(3000)

        # 1. 核对当前页面是否已选中该作品，未选中则通过「选择视频」抽屉精准定位（严格防错位）
        def _norm(s: str) -> str:
            return re.sub(r"[【】《》#\s\-_，。！？、“”\"'']", "", s or "").lower()

        def _same_work(shown: str) -> bool:
            """两侧都必须非空且足够长才比较 —— 空串 in 任意串 恒为真，
            页面标题还没渲染时会把「当前默认选中的上一条作品」误判成目标。

            只做**同向前缀**比对，不做双向包含：`cur[:8] in clean_target` 会让
            「这操作太离谱了」在目标「这操作太离谱了吧」里命中，系列/模板标题
            会把首评发到上一条作品 —— 正是本函数要防的事。
            """
            cur = _norm(shown)
            if len(cur) < 4:
                return False
            return cur.startswith(clean_target) or clean_target.startswith(cur[:8])

        if clean_target:
            matched = False
            for attempt in range(4):
                try:
                    cur_title_el = page.locator(
                        ".video-info__content__title, [class*='video-info'] [class*='title']").first
                    cur_title = cur_title_el.inner_text() if cur_title_el.count() else ""
                    if _same_work(cur_title):
                        matched = True
                        break
                except Exception:
                    pass

                # 主卡片不一致，打开「选择视频」抽屉定位
                select_btn = page.get_by_text("选择视频")
                if select_btn.count() and select_btn.is_visible():
                    try:
                        select_btn.click()
                        page.wait_for_timeout(1500)
                        items = page.locator(
                            ".drawer .video-info, [class*='drawer'] .video-info").all()
                        for it in items:
                            if _same_work(it.inner_text()):
                                it.click()
                                page.wait_for_timeout(2000)
                                # 点选后必须回读主卡片二次确认，不能「看着像」就当已切换
                                try:
                                    after_el = page.locator(
                                        ".video-info__content__title, "
                                        "[class*='video-info'] [class*='title']").first
                                    after = after_el.inner_text() if after_el.count() else ""
                                    matched = _same_work(after)
                                except Exception:
                                    matched = False
                                break
                    except Exception as e:
                        log.debug("抽屉选择视频异常: %s", e)

                if matched:
                    break

                page.wait_for_timeout(3000)
                try:
                    page.reload(wait_until="domcontentloaded")
                except Exception:
                    pass
                page.wait_for_timeout(2000)

            if not matched:
                log.warning("评论管理页未匹配到目标视频「%s」，放弃发表首评（严格防止错位发到历史作品）", clean_target)
                return False

        # 2. 找到评论输入框
        box = page.locator(".author-comment-input__row__input, [placeholder*='有爱评论']").first
        if not (box.count() and box.is_visible()):
            log.warning("未找到作者评论输入框，跳过首评")
            return False

        box.click()
        page.wait_for_timeout(300)
        page.keyboard.type(first_comment, delay=30)
        page.wait_for_timeout(500)

        # 3. 找到并点击「发送」按钮（严格断言与回读校验）
        # 必须等待 Vue 输入状态响应式更新（去除 --disabled 类名），且绝对不能用 text=发送（会误选外层 968px 容器）
        sent_ok = False
        btn = page.locator(".author-comment-input__row__btn").first

        def _comment_shown() -> bool:
            return bool(
                page.locator(".comment-item, [class*='comment-item']")
                .filter(has_text=first_comment[:8]).count())

        for send_attempt in range(1, 4):
            if send_attempt > 1:
                # 重试前必须确认「上一次真的没发出去」：评论列表渲染可能比接口慢，
                # 盲目再点一次发送会在视频下留下两条一模一样的置顶首评。
                if _comment_shown():
                    sent_ok = True
                    break
                box_text = ""
                try:
                    box_text = (box.inner_text() or "").strip()
                except Exception:
                    pass
                if not box_text:
                    # 输入框已被清空 ⇒ 上一次点击其实生效了，等渲染即可
                    log.info("首评输入框已清空，等待评论渲染（不再重复点击发送）")
                    page.wait_for_timeout(2500)
                    sent_ok = _comment_shown()
                    break

            page.wait_for_timeout(600)
            if btn.count() and btn.is_visible():
                btn_cls = btn.get_attribute("class") or ""
                if "disabled" not in btn_cls:
                    btn.click(force=True)
                else:
                    # 若仍带 disabled，点击一下输入框触发 input 事件再试
                    box.click()
                    page.keyboard.press("Space")
                    page.keyboard.press("Backspace")
                    page.wait_for_timeout(300)
                    btn.click(force=True)

            page.wait_for_timeout(2000)
            if _comment_shown():
                sent_ok = True
                break
            log.warning("作者首评未出现在评论区，重试点击发送 (第 %d 次)...", send_attempt)

        shot(page, "ks_comment_sent")
        if not sent_ok:
            log.warning("作者首评发送后未在评论区出现，放弃置顶")
            return False

        log.info("作者首评已发送: %s", first_comment)

        # 4. 置顶评论（如果 pin=True）
        if pin:
            try:
                page.wait_for_timeout(1000)
                # 严格锁定到「本条款首评」所在行；找不到就放弃置顶。
                # 曾经回退到 page.locator("text=置顶").first —— 那是页面上
                # 任意一条评论的置顶按钮，会把别的评论顶上去。
                target_item = page.locator(".comment-item, [class*='comment-item']").filter(
                    has_text=first_comment[:8]).first
                if not target_item.count():
                    log.warning("未定位到刚发表的首评行，放弃置顶（避免置错评论）")
                    return True
                pin_btn = target_item.locator("text=置顶").first
                if pin_btn.count() and pin_btn.is_visible():
                    pin_btn.click()
                    page.wait_for_timeout(1000)
                    confirm_btn = page.locator(
                        ".cp-dialog-wrapper button:has-text('确认'), .cp-dialog button:has-text('确认'), "
                        ".el-dialog button:has-text('确定'), .el-dialog button:has-text('确认'), "
                        ".el-message-box button:has-text('确定'), button:has-text('确认'), button:has-text('确定')"
                    ).first
                    if confirm_btn.count() and confirm_btn.is_visible():
                        confirm_btn.click()
                        page.wait_for_timeout(1000)
                    log.info("作者首评已成功置顶: %s", first_comment)
                    shot(page, "ks_comment_pinned")
            except Exception as e:
                log.debug("置顶评论失败（不影响已发送评论）: %s", e)

        return True
    except Exception as e:
        log.warning("发表作者首评异常（不影响视频发布）: %s", e)
        shot(page, "ks_comment_fail")
        return False

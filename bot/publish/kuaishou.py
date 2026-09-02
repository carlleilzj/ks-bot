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

from ..config import DATA_DIR, KS_STATE_PATH
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
)

log = logging.getLogger(__name__)

PUBLISH_URL = "https://cp.kuaishou.com/article/publish/video"
MANAGE_URL = "https://cp.kuaishou.com/article/manage"
SPARK_RR_PATH = DATA_DIR / "ks_spark_rr.json"
SPARK_TYPE_LABEL = "关联变现任务"

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
    "upload_done_texts": ["重新上传", "上传完成", "转码完成"],
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

def _is_logged_in(page: Page) -> bool:
    """发布页上的快速判定：不在登录页且能找到上传入口。"""
    try:
        if "passport.kuaishou.com" in page.url:
            return False
        return page.locator(SELECTORS["file_input"]).count() > 0
    except Exception:
        return False


def _has_login_cookies(context) -> bool:
    """按 cookie 判定登录态（不依赖页面结构，扫码成功即生效）。"""
    try:
        cookies = context.cookies(["https://cp.kuaishou.com"])
        names = {c["name"] for c in cookies}
        return "userId" in names or "passToken" in names
    except Exception:
        return False


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
            page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
            settle(page)
            if not (_is_logged_in(page) or _has_login_cookies(context)):
                shot(page, "ks_login_expired")
                raise LoginExpired("快手登录态已失效，请运行 python -m bot.main --login kuaishou 重新扫码")

            # 0. 关闭「继续编辑上次未发布视频」弹窗（如果有）
            dismiss_dialogs(page)

            # 1. 上传视频
            file_input = page.locator(SELECTORS["file_input"]).first
            file_input.set_input_files(str(video))
            log.info("已提交视频上传：%s", video.name)

            # 2. 等待上传 + 转码完成
            wait_upload_done(page, SELECTORS["upload_done_texts"], SELECTORS["upload_fail_texts"],
                             shot_prefix="ks", timeout=UPLOAD_TIMEOUT)
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

            # 5.5 挂星火「关联变现任务」（需 App 先收藏；失败不阻断发布）
            if spark_task:
                _attach_spark_task(page, prefer=spark_task_title)

            # 6. 点击发布
            _click_publish(page)

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
    try:
        return page.evaluate("""() => [...document.querySelectorAll(
            '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content'
        )].map(e => (e.textContent || '').trim()).filter(Boolean)""") or []
    except Exception:
        return []


def _click_visible_option(page: Page, text: str) -> bool:
    """点开着的 ant-select 下拉里标题完全匹配的项。"""
    try:
        ok = page.evaluate("""(want) => {
            const nodes = [...document.querySelectorAll(
                '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option'
            )];
            const el = nodes.find(e => ((e.textContent || '').trim()) === String(want || '').trim());
            if (!el) return false;
            el.scrollIntoView({block: 'nearest'});
            el.click();
            return true;
        }""", text)
        return bool(ok)
    except Exception:
        return False


def _click_placeholder(page: Page, placeholder: str) -> bool:
    loc = page.get_by_text(placeholder, exact=True)
    try:
        if loc.count():
            loc.last.click(force=True, timeout=2500)
            return True
    except Exception:
        pass
    return False


def _attach_spark_task(page: Page, prefer: str = "") -> str | None:
    """发布表单挂星火「关联变现任务」。成功返回任务标题，失败返回 None（不抛）。"""
    try:
        dismiss_dialogs(page)
        _remove_joyride(page)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(400)

        if not _click_placeholder(page, "选择服务类型"):
            log.info("未找到「选择服务类型」，跳过星火挂载")
            return None
        page.wait_for_timeout(800)
        opts = _dropdown_option_texts(page)
        if SPARK_TYPE_LABEL not in opts and SPARK_TYPE_LABEL not in (page.inner_text("body") or ""):
            log.info("作者服务下拉里没有「关联变现任务」，跳过星火挂载")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return None
        if not _click_visible_option(page, SPARK_TYPE_LABEL):
            # 下拉项可能还没套 ant-select-item class，退回文案点击
            loc = page.get_by_text(SPARK_TYPE_LABEL, exact=True)
            if not loc.count():
                log.info("点不开「关联变现任务」，跳过星火挂载")
                return None
            loc.last.click(force=True, timeout=2500)
        page.wait_for_timeout(1200)

        # 选完类型后，右侧下拉从 disabled 变成「关联变现任务获得更多收入」
        second_ph = "关联变现任务获得更多收入"
        deadline = time.time() + 8
        opened = False
        while time.time() < deadline:
            if _click_placeholder(page, second_ph):
                opened = True
                break
            page.wait_for_timeout(400)
        if not opened:
            log.info("星火任务下拉未出现（可能还没在 App 收藏任务），跳过挂载")
            return None
        page.wait_for_timeout(800)

        titles = _dropdown_option_texts(page)
        if not titles:
            log.info("星火收藏任务列表为空，跳过挂载")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return None

        chosen, nxt = pick_spark_title(titles, prefer=prefer, cursor=_spark_rr_load())
        if not chosen:
            return None
        if not _click_visible_option(page, chosen):
            loc = page.get_by_text(chosen, exact=True)
            if loc.count():
                loc.last.click(force=True, timeout=2500)
            else:
                log.warning("星火任务「%s」在下拉里点不到，跳过挂载", chosen)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                return None
        _spark_rr_save(nxt)
        page.wait_for_timeout(600)
        log.info("已挂星火变现任务：%s", chosen)
        return chosen
    except Exception as e:
        log.warning("挂星火变现任务失败（不影响发布）：%s", e)
        shot(page, "ks_spark_attach_fail")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return None


def _upload_cover(page: Page, cover: Path) -> None:
    try:
        for text in ("编辑封面", "修改封面", "更换封面", "设置封面"):
            btn = page.get_by_text(text, exact=False).first
            if btn.count() and btn.is_visible():
                btn.click()
                rand_sleep()
                up = page.get_by_text("上传封面", exact=False).first
                if up.count() and up.is_visible():
                    up.click()
                    rand_sleep()
                img_input = page.locator(SELECTORS["cover_input"]).first
                if img_input.count():
                    img_input.set_input_files(str(cover))
                    rand_sleep(1.0, 2.0)
                    for t in ("完成", "确定"):
                        b = page.get_by_text(t, exact=True).first
                        if b.count() and b.is_visible():
                            b.click()
                            break
                    log.info("已上传自定义封面")
                    return
        log.info("未找到封面上传入口，使用平台自动封面")
    except Exception as e:
        log.warning("封面上传失败（不影响发布）：%s", e)
        shot(page, "ks_cover_fail")


def _click_publish(page: Page) -> None:
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

    # 在同一次 JS evaluate 内注入 CSS 屏蔽 joyride + 查找按钮并点击
    clicked = page.evaluate("""() => {
        if (!document.getElementById('__ks_joyride_killer')) {
            const s = document.createElement('style');
            s.id = '__ks_joyride_killer';
            s.textContent = '#react-joyride-portal, .react-joyride__overlay, .react-joyride__spotlight { pointer-events: none !important; z-index: -1 !important; }';
            document.head.appendChild(s);
        }
        const els = [...document.querySelectorAll('*')].filter(el => {
            const t = (el.textContent || '').trim();
            const cls = (el.className || '').toString();
            return t === '发布' && cls.includes('button-primary');
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
            page.get_by_role("button", name=SELECTORS["publish_btn_text"], exact=True),
            page.locator("button", has_text=re.compile(r"^\s*发布\s*$")),
            page.locator("button.button-primary", has_text=re.compile(r"发布")),
        ):
            try:
                if loc.count():
                    loc.first.click(force=True)
                    clicked = True
                    break
            except Exception:
                continue
    if not clicked:
        shot(page, "ks_publish_btn_fail")
        raise KuaishouError("未找到发布按钮，截图见 logs/（页面可能已改版）")
    log.info("已点击发布按钮")

    # 注意：pc/submit 返回 200 不代表发布成功——账号被风控时接口照常 200 但作品被拦截。
    # 可靠标志：URL 跳转到内容管理页（?status=2 审核中）+ 页面出现「审核中」。
    deadline = time.time() + 45
    jumped = False
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        # 风控/违规拦截提示（仅当已跳转管理页或弹窗出现时检查；「未通过」可能是筛选 tab 文字）
        for text in ("发布失败", "违规", "操作过于频繁", "请稍后再试"):
            if page.get_by_text(text, exact=False).count():
                shot(page, "ks_publish_blocked")
                raise KuaishouError(
                    f"快手发布被拦截（页面提示「{text}」）。"
                    f"若创作者中心显示账号健康，优先检查小火箭分流是否把 *.kuaishou.com 走了代理"
                    f"（发布浏览器应直连国内站）"
                )
        for text in SELECTORS["success_texts"]:
            if page.get_by_text(text, exact=False).count():
                return
        if "/article/manage" in page.url:
            jumped = True
            return
    # 未检测到跳转：打开内容管理页核实最新作品是否出现（发布可能成功但 SPA 未更新 URL）
    if not jumped:
        try:
            page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            body = page.evaluate("() => document.body.innerText")
            if "审核中" in body or "已发布" in body:
                log.info("内容管理页已出现新作品（发布成功）")
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

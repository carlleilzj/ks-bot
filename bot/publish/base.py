"""发布平台公共底座：异常基类 + Playwright 通用工具。

各平台模块（kuaishou/douyin/xhs/weixin）从这里复用：
- 异常：PublishError（可重试错误）、LoginExpired（需人工重新扫码）
- 工具：launch_chromium（发布浏览器强制直连）、new_context、
  settle、随机延时、失败截图、弹窗清理、上传/转码轮询
"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page

from ..config import LOGS_DIR

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 上传+转码等待上限（秒），各平台一致
UPLOAD_TIMEOUT = 15 * 60

# 小火箭 / 系统代理常把这些变量塞进进程；发布国内创作者中心必须直连
PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)

# Chromium --proxy-bypass-list（PUBLISH_PROXY 有值时生效）
PUBLISH_PROXY_BYPASS = [
    "<-loopback>",
    "*.kuaishou.com", "*.gifshow.com", "*.kwcdn.com",
    "*.douyin.com", "*.bytedance.com", "*.byteimg.com",
    "*.xiaohongshu.com", "*.xhscdn.com",
    "*.weixin.qq.com", "*.qq.com",
]


class PublishError(RuntimeError):
    """发布过程错误（上传失败/页面改版/超时等，可重试）。"""


class LoginExpired(PublishError):
    """登录态缺失或失效，需要人工重新扫码。"""


# ---------- 发布浏览器（默认直连，避开小火箭环境代理） ----------

def chromium_launch_env(src: dict | None = None) -> dict:
    """复制环境变量并去掉代理相关项，避免 Chromium 继承 HTTP_PROXY。"""
    env = dict(os.environ if src is None else src)
    for key in PROXY_ENV_KEYS:
        env.pop(key, None)
    return env


def chromium_launch_kwargs(*, headless: bool = True, slow_mo: int | None = None,
                           proxy: str | None = None) -> dict:
    """Playwright chromium.launch / launch_persistent_context 的公共参数。

    PUBLISH_PROXY 为空（默认）：--no-proxy-server，强制直连国内站。
    有值：走该代理，同时 bypass 国内创作者域名。
    """
    if proxy is None:
        proxy = os.environ.get("PUBLISH_PROXY", "").strip()
    args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    kwargs: dict = {
        "headless": headless,
        "args": args,
        "env": chromium_launch_env(),
    }
    if slow_mo:
        kwargs["slow_mo"] = slow_mo
    if proxy:
        kwargs["proxy"] = {"server": proxy}
        args.append("--proxy-bypass-list=" + ";".join(PUBLISH_PROXY_BYPASS))
        log.info("发布浏览器走 PUBLISH_PROXY=%s（国内创作者域名 bypass）", proxy)
    else:
        args.append("--no-proxy-server")
        log.info("发布浏览器强制直连（忽略 HTTP_PROXY / 小火箭环境代理）")
    return kwargs


def launch_chromium(playwright, *, headless: bool = True, slow_mo: int | None = None,
                    proxy: str | None = None):
    """启动用于登录/发布的 Chromium。"""
    return playwright.chromium.launch(
        **chromium_launch_kwargs(headless=headless, slow_mo=slow_mo, proxy=proxy)
    )


def launch_persistent_chromium(playwright, user_data_dir: str | Path, *,
                               headless: bool = False, slow_mo: int | None = None,
                               proxy: str | None = None, **extra):
    """带持久化 profile 的 Chromium（小红书登录用）。"""
    kwargs = chromium_launch_kwargs(headless=headless, slow_mo=slow_mo, proxy=proxy)
    kwargs.update(extra)
    return playwright.chromium.launch_persistent_context(str(user_data_dir), **kwargs)


# ---------- 基础工具 ----------

def shot(page: Page, name: str) -> None:
    """失败截图到 logs/（尽力而为，绝不抛错）。"""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        page.screenshot(path=str(LOGS_DIR / f"{name}_{ts}.png"), full_page=True)
    except Exception:
        pass


def rand_sleep(a: float = 0.6, b: float = 1.6) -> None:
    time.sleep(random.uniform(a, b))


def new_context(browser, state_path: Path):
    """带登录态/UA/时区的浏览器 context，弱化自动化特征。"""
    kwargs = dict(
        user_agent=UA,
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )
    # 只加载非空文件：空文件/损坏文件会导致 Playwright JSON 解析崩溃
    if Path(state_path).exists() and Path(state_path).stat().st_size > 2:
        kwargs["storage_state"] = str(state_path)
    context = browser.new_context(**kwargs)
    context.add_init_script("""
        // 弱化自动化特征
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        // 禁用 react-joyride 引导遮罩的点击拦截（不删除 DOM，避免 React 重渲染）
        const style = document.createElement('style');
        style.id = '__ks_joyride_killer';
        style.textContent = [
            '#react-joyride-portal, .react-joyride__overlay, .react-joyride__spotlight',
            '{ pointer-events: none !important; z-index: -1 !important; }',
        ].join('');
        document.head.appendChild(style);
        new MutationObserver(() => {
            if (!document.getElementById('__ks_joyride_killer')) {
                const s = document.createElement('style');
                s.id = '__ks_joyride_killer';
                s.textContent = '#react-joyride-portal, .react-joyride__overlay, .react-joyride__spotlight { pointer-events: none !important; z-index: -1 !important; }';
                document.head.appendChild(s);
            }
        }).observe(document.head, {childList: true, attributes: true, subtree: true});
    """)
    return context


def settle(page: Page, seconds: float = 3.0) -> None:
    """等页面脚本渲染完。"""
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    time.sleep(seconds)


def dismiss_dialogs(page: Page, extra_texts: tuple[str, ...] = ()) -> None:
    """关闭各种弹窗：继续编辑/新手引导/引导浮层等。"""
    # 1. JS 移除 react-joyride 等新手引导遮罩（空 tooltip 但会遮挡表单/拦截点击）
    _remove_joyride(page)

    # 2. 关闭常规弹窗
    dismiss_texts = ("放弃", "稍后", "取消", "跳过", "我知道了", "关闭", "不再提示",
                     "我知道啦", "下一步", "完成", "知道了", "跳过引导") + tuple(extra_texts)
    try:
        for btn_text in dismiss_texts:
            btn = page.get_by_role("button", name=btn_text, exact=True)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                rand_sleep(0.5)
                log.info("已关闭弹窗（%s）", btn_text)
    except Exception:
        pass


def wait_upload_done(page: Page, done_texts: list[str], fail_texts: list[str],
                     shot_prefix: str, timeout: int = UPLOAD_TIMEOUT) -> None:
    """轮询等待上传+转码完成；出现失败文案立即报错。"""
    deadline = time.time() + timeout
    last_log = 0.0
    min_wait = time.time() + 3  # 至少等 3 秒，避免弹窗文字误匹配
    while time.time() < deadline:
        for text in fail_texts:
            if page.get_by_text(text, exact=False).count():
                shot(page, f"{shot_prefix}_upload_fail")
                raise PublishError(f"视频上传失败（页面出现「{text}」），截图见 logs/")
        if time.time() > min_wait:
            for text in done_texts:
                if page.get_by_text(text, exact=False).count():
                    log.info("上传/转码完成")
                    return
        if time.time() - last_log > 30:
            log.info("等待上传与转码...（最长 %d 分钟）", timeout // 60)
            last_log = time.time()
        time.sleep(5)
    shot(page, f"{shot_prefix}_upload_timeout")
    raise PublishError(f"等待上传/转码超时（{timeout // 60} 分钟），截图见 logs/")


def _remove_joyride(page: Page) -> None:
    """禁用 react-joyride 新手引导遮罩的点击拦截（注入 CSS 而非删除 DOM，避免 React 重渲染）。"""
    try:
        style_id = "__ks_joyride_killer"
        page.evaluate("""(id) => {
            if (!document.getElementById(id)) {
                const s = document.createElement('style');
                s.id = id;
                s.textContent = '#react-joyride-portal, .react-joyride__overlay, .react-joyride__spotlight { pointer-events: none !important; z-index: -1 !important; }';
                document.head.appendChild(s);
            }
        }""", style_id)
    except Exception:
        pass


def fill_editor(page: Page, text: str, shot_prefix: str, candidates: list | None = None) -> None:
    """向 contenteditable/textarea 填文案（清空原有内容后输入）。"""
    candidates = candidates or [
        page.locator('div[contenteditable="true"]').first,
        page.locator('textarea').first,
    ]
    deadline = time.time() + 15
    while time.time() < deadline:
        _remove_joyride(page)  # 引导浮层会拦截点击，每次尝试前先清掉
        for loc in candidates:
            try:
                if loc.count():
                    # force=True：跳过可点击性检查（joyride 遮挡时 click 会卡死等待），直接派发
                    loc.first.click(force=True)
                    rand_sleep(0.3, 0.8)
                    page.keyboard.press("Control+a")
                    page.keyboard.press("Backspace")
                    page.keyboard.type(text, delay=30)
                    log.info("已填写文案（%d 字）", len(text))
                    return
            except Exception:
                continue
        time.sleep(1)
    shot(page, f"{shot_prefix}_fill_fail")
    raise PublishError("未找到文案输入框，截图见 logs/（页面可能已改版，请更新 SELECTORS）")

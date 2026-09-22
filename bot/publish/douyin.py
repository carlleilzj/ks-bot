"""抖音创作者中心（creator.douyin.com）Playwright 自动化：扫码登录 + 上传发布。

注意：
- 抖音发布页无「分区」概念，AI 生成的话题标签（#xxx）直接写进简介
- 标题是简介编辑器的第一行，上限 55 字（文案层已按此约束生成）
- 滑块/验证码风控：检测到验证元素立即截图报错，TG 通知人工处理
- 选择器集中在 SELECTORS，页面改版后只需调整这里
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from ..config import DATA_DIR
from .douyin_anchor import apply_anchors, apply_hot_topic, pick_goods_for
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
    goto_with_retry,
    reload_with_retry,
)

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "douyin_state.json"

PUBLISH_URL = "https://creator.douyin.com/creator-micro/content/upload"
LOGIN_HINT = "passport.douyin.com"

UPLOAD_TIMEOUT = 15 * 60

SELECTORS = {
    "file_input": "input[type='file']",
    "editor": "div[contenteditable='true']",
    # 标题是独立 input（semi-input），2026-09 改版后简介编辑器不再承载标题
    "title_input": "input[placeholder*='标题']",
    "cover_input": "input[type='file'][accept*='image']",
    "publish_btn_text": "发布",
    "upload_done_texts": ["上传完成", "已上传完成"],
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


def _cookie_file_has_login(state_path: Path) -> bool:
    """直接读 storage_state 文件判登录 cookie（不依赖 Playwright 域匹配）。"""
    import time as _time
    try:
        data = json.loads(Path(state_path).read_text())
        now = _time.time()
        names = set()
        for c in data.get("cookies", []):
            exp = c.get("expires", -1)
            if exp is None or exp < 0 or exp > now:
                names.add(c.get("name", ""))
        return "sessionid" in names or "sessionid_ss" in names
    except Exception:
        return False


def _has_login_cookies(context) -> bool:
    try:
        cookies = context.cookies(["https://creator.douyin.com", "https://www.douyin.com"])
        names = {c["name"] for c in cookies}
        if "sessionid" in names or "sessionid_ss" in names:
            return True
    except Exception:
        pass
    return _cookie_file_has_login(STATE_PATH)


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
    cover: Path | None = None,
    headless: bool = True,
    state_path: Path = STATE_PATH,
    manual_verify: bool = False,  # True=有头模式，短信验证弹窗由人工在窗口里完成
    anchors: dict | None = None,      # 挂载标签 {"标记万物": "xxx", "位置": "yyy", ...}
    hot_topic: str = "",              # 关联热点词
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
            goto_with_retry(page, PUBLISH_URL)
            settle(page)
            _check_captcha(page, "打开发布页")
            if not (_is_logged_in(page) or _has_login_cookies(context)):
                shot(page, "dy_login_expired")
                raise LoginExpired("抖音登录态已失效，请运行 python -m bot.main --login douyin 重新扫码")

            dismiss_dialogs(page)

            # 1+2. 上传视频并等待转码完成（网络抖动导致失败自动重传）
            _upload_with_retry(page, video)
            page.wait_for_timeout(5000)
            _check_captcha(page, "上传后")
            dismiss_dialogs(page)
            rand_sleep()

            # 3. 填标题+简介+话题
            #    抖音改版后标题为独立 input（placeholder='填写作品标题，为作品获得更多流量'），
            #    简介为 contenteditable（placeholder='添加作品简介'）。两者分开填。
            title_input = page.locator(SELECTORS["title_input"]).first
            try:
                title_input.click(force=True)
                title_input.fill(title)
                log.info("已填写标题（%d 字）", len(title))
            except Exception as e:
                log.warning("标题输入框填写失败（可能改版）：%s", str(e)[:100])
            editor = page.locator(SELECTORS["editor"]).first
            fill_editor(page, content, shot_prefix="dy_desc", candidates=[editor])

            # 3.4 上传自定义封面（横 4:3 + 竖 3:4）。失败不阻断发布。
            if cover and Path(cover).exists():
                _upload_cover(page, Path(cover))

            # 3.5 挂载标签（商品/位置/小程序/团购/热点），失败不阻塞发布
            if anchors or hot_topic:
                try:
                    # 商品/标记万物 = auto → 全国日用商品（默认抽纸）
                    _anchors = dict(anchors or {})
                    for k in ("标记万物", "商品", "团购"):
                        if str(_anchors.get(k, "")).lower() in ("auto", "全国"):
                            _anchors[k] = pick_goods_for(
                                f"{title} {description} {' '.join(tags or [])}")
                            log.info("自动选品（%s）：%s", k, _anchors[k])
                    done = apply_anchors(page, _anchors, hot_topic)
                    if done:
                        log.info("抖音挂载完成：%s", "/".join(done))
                    else:
                        log.warning("抖音挂载未生效（配置=%r 热点=%r）", anchors, hot_topic)
                    shot(page, "dy_anchors_done")
                except Exception as e:
                    log.warning("挂载流程异常（继续发布）：%s", str(e)[:150])
                _dismiss_douyin_modals(page)

            # 4. 点发布（manual_verify=True 时短信验证弹窗由人工完成）
            _click_publish(page, manual_verify=manual_verify, title=title)

            dy_url = _fetch_dy_url(context)
            shot(page, "dy_publish_done")
            log.info("抖音发布成功：%s", dy_url or "（作品链接获取失败，见创作者中心-内容管理）")
            return dy_url
        finally:
            context.close()
            browser.close()


def _upload_cover(page: Page, cover: Path) -> None:
    """给抖音横 4:3 / 竖 3:4 两个封面位塞同一张图。

    平台自动封面会吃到 YouTube 片头黑帧；自定义封面必须显式上传。
    失败不阻断发布。
    """
    try:
        img_inputs = page.locator(SELECTORS["cover_input"])
        n = img_inputs.count()
        if n == 0:
            # 点「选择封面 / 设置封面 / 上传封面」再找 file input
            for text in ("选择封面", "设置封面", "上传封面", "更换封面"):
                btn = page.get_by_text(text, exact=False)
                if btn.count():
                    try:
                        btn.first.click(timeout=1500)
                        rand_sleep(0.4, 0.8)
                    except Exception:
                        pass
            img_inputs = page.locator(SELECTORS["cover_input"])
            n = img_inputs.count()
        if n == 0:
            log.info("未找到抖音封面上传入口，使用平台自动封面")
            return
        uploaded = 0
        for i in range(min(n, 2)):
            try:
                img_inputs.nth(i).set_input_files(str(cover))
                uploaded += 1
                rand_sleep(0.6, 1.2)
            except Exception as e:
                log.debug("抖音封面位 %d 上传失败：%s", i, str(e)[:80])
        for t in ("完成", "确定", "确认"):
            b = page.get_by_text(t, exact=True).first
            if b.count() and b.is_visible():
                try:
                    b.click(timeout=1500)
                    break
                except Exception:
                    pass
        if uploaded:
            log.info("已上传抖音自定义封面（%d 个封面位）", uploaded)
        else:
            log.info("抖音封面位存在但未能写入文件，使用平台自动封面")
    except Exception as e:
        log.warning("抖音封面上传失败（不影响发布）：%s", e)
        shot(page, "dy_cover_fail")


def _sms_dialog_present(page: Page) -> bool:
    """是否弹出「接收短信验证码」风控弹窗（发布被拦，等人工输入验证码）。"""
    try:
        for text in ("接收短信验证码", "请输入当前手机号", "短信验证码"):
            if page.get_by_text(text, exact=False).count():
                return True
    except Exception:
        pass
    return False


def _wait_file_input_with_retry(page: Page, max_refresh: int = 3) -> None:
    """等上传输入框出现；遇到'页面出错了'自动刷新重试。

    抖音创作者页偶发前端加载失败（主内容区显示"页面出错了，刷新试试"），
    刷新即可恢复。不处理的话 wait_for_selector 60s 超时 → 误报失败 → 重复发布。
    """
    for refresh in range(max_refresh + 1):
        try:
            page.wait_for_selector(SELECTORS["file_input"], state="attached", timeout=20_000)
            return  # 出现了
        except Exception:
            pass
        # 20s 还没出现：看是不是'页面出错了'状态
        try:
            body = page.locator("body").inner_text(timeout=5_000)
            page_broken = "页面出错了" in body or "刷新试试" in body
        except Exception:
            page_broken = False
        if not page_broken and refresh < max_refresh:
            # 没有明确错误提示，也可能只是慢；再等一轮
            try:
                page.wait_for_selector(SELECTORS["file_input"], state="attached", timeout=20_000)
                return
            except Exception:
                pass
        if refresh < max_refresh:
            log.warning("发布页加载失败（第 %d 次），自动刷新重试", refresh + 1)
            shot(page, f"dy_page_broken_{refresh + 1}")
            reload_with_retry(page)
            page.wait_for_timeout(5000)
    # 最后一轮完整等待
    page.wait_for_selector(SELECTORS["file_input"], state="attached", timeout=60_000)


def _wait_dy_upload_done(page: Page, timeout: int = UPLOAD_TIMEOUT) -> None:
    """等待抖音视频上传完成。"""
    deadline = time.time() + timeout
    last_log = 0.0
    time.sleep(2)
    while time.time() < deadline:
        for fail in SELECTORS["upload_fail_texts"]:
            if page.get_by_text(fail, exact=False).count():
                shot(page, "dy_upload_fail")
                raise DouyinError(f"视频上传失败（页面出现「{fail}」）")
        try:
            body = page.locator("body").inner_text(timeout=3000)
        except Exception:
            body = ""
        # 上传完成标志：出现「重新上传」或「上传成功」，且不再处于「取消上传」/上传中阶段
        has_done = "重新上传" in body or "上传成功" in body or "已上传完成" in body
        is_uploading = "取消上传" in body or "上传过程中" in body or ("已上传：" in body and "%" in body)
        if has_done and not is_uploading:
            log.info("抖音视频上传完成")
            return
        if time.time() - last_log > 15:
            log.info("等待抖音视频上传...（最长 %d 分钟）", timeout // 60)
            last_log = time.time()
        time.sleep(2)
    shot(page, "dy_upload_timeout")
    raise DouyinError(f"等待抖音上传超时（{timeout // 60} 分钟）")


def _upload_with_retry(page: Page, video, max_attempts: int = 3) -> None:
    """上传视频；网络抖动导致的上传失败/超时自动重传。

    家庭机 fanout VPN 接口高频抖动会打断上传传输（页面报「上传失败」），
    重新选文件即可恢复。
    """
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            _wait_file_input_with_retry(page)
            file_input = page.locator(SELECTORS["file_input"]).first
            file_input.set_input_files(str(video))
            log.info("已提交视频上传：%s（第 %d 次）", video.name, attempt)

            _wait_dy_upload_done(page, timeout=UPLOAD_TIMEOUT)
            return  # 成功
        except PublishError as e:
            last_err = e
            if attempt >= max_attempts:
                break
            log.warning("上传失败（第 %d/%d 次）：%s，准备重传", attempt, max_attempts, str(e)[:120])
            # 回到干净的发布页再重传
            try:
                goto_with_retry(page, PUBLISH_URL)
                page.wait_for_timeout(5000)
                dismiss_dialogs(page)
            except Exception as e2:
                log.warning("重传前刷新页面失败：%s", str(e2)[:100])
    raise last_err if last_err else PublishError("视频上传失败（重传耗尽）")


def _dismiss_douyin_modals(page: Page) -> None:
    """关闭可能遮挡发布按钮的弹窗（账号违规提示、权限收回、游戏推广等）。"""
    # 1. 账号违规 / 权限收回居中弹窗
    try:
        violation = page.locator("div, [class*='modal']").filter(has_text=re.compile(r"(你的账号违规|权限被收回|违规记录)"))
        if violation.count():
            close_btn = violation.first.locator("button[aria-label*='Close'], .semi-modal-close, [class*='close'], svg").first
            if close_btn.count() and close_btn.is_visible():
                close_btn.click()
                page.wait_for_timeout(1000)
                log.info("已关闭抖音账号违规/权限提示弹窗")
    except Exception as e:
        log.debug("关闭违规弹窗异常：%s", e)

    # 2. 挂载游戏手柄后弹出「游戏推广」开通弹窗
    try:
        body = page.locator("body").inner_text(timeout=3000)
    except Exception:
        body = ""
    if "游戏推广" in body or "暂不开通" in body:
        for label in ("暂不开通，仅添加", "暂不开通", "仅添加"):
            try:
                loc = page.get_by_role("button", name=label, exact=False)
                if not loc.count():
                    loc = page.get_by_text(label, exact=False)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=3000)
                    page.wait_for_timeout(1000)
                    log.info("已关闭游戏推广弹窗（%s）", label)
                    return
            except Exception:
                continue
        try:
            x = page.get_by_text("游戏推广", exact=False).locator(
                "xpath=ancestor::*[contains(@class,'modal') or contains(@class,'dialog') or contains(@class,'popup')][1]//button"
            )
            if x.count():
                x.first.click(timeout=2000)
                page.wait_for_timeout(1000)
                log.info("已点游戏推广弹窗关闭按钮")
        except Exception:
            pass


def _click_publish(page: Page, manual_verify: bool = False, title: str = "") -> None:
    _dismiss_douyin_modals(page)
    dismiss_dialogs(page, extra_texts=("暂不开通，仅添加", "暂不开通"))
    page.wait_for_timeout(800)
    candidates = [
        page.get_by_role("button", name=SELECTORS["publish_btn_text"], exact=True),
        page.locator("button", has_text=re.compile(r"^\s*发\s*布\s*$")),
        page.get_by_text(SELECTORS["publish_btn_text"], exact=True),
    ]
    clicked = False
    for loc in candidates:
        try:
            n = loc.count()
            if not n:
                continue
            # 底部「发布」通常是最后一个可见 button
            target = loc.nth(n - 1) if n > 1 else loc.first
            if not target.is_visible():
                target = loc.first
            try:
                target.click(timeout=5000)
            except Exception:
                target.click(force=True, timeout=5000)
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        shot(page, "dy_publish_btn_fail")
        raise DouyinError("未找到发布按钮，截图见 logs/（抖音页面可能已改版）")

    time.sleep(3)

    # 再次清理可能弹出的违规/拦截提示
    _dismiss_douyin_modals(page)

    # 验证码检查（只查一次，不轮询——避免在跳转页操作 DOM 崩溃）
    if _has_captcha(page):
        shot(page, "dy_captcha_on_publish")
        raise DouyinError("抖音在点击发布时触发滑块验证，截图见 logs/，请人工过验证或稍后重试")

    # 短信验证检查
    if _sms_dialog_present(page):
        if manual_verify:
            shot(page, "dy_sms_verify")
            log.info("抖音要求短信验证，请在浏览器窗口输入验证码并点击「验证」（5 分钟内有效）")
            sms_deadline = time.time() + 300
            while time.time() < sms_deadline:
                if not _sms_dialog_present(page):
                    break
                time.sleep(2)
        else:
            shot(page, "dy_sms_verify")
            raise DouyinError("抖音发布触发短信验证（风控），需人工在浏览器里输入验证码完成发布；"
                              "自动流程已中止，本条会重试")

    # 等待页面跳转到内容管理页或出现成功文案
    deadline = time.time() + 45
    jumped = False
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        _dismiss_douyin_modals(page)
        dismiss_dialogs(page)
        for text in SELECTORS["success_texts"]:
            if page.get_by_text(text, exact=False).count():
                log.info("检测到抖音发布成功文字：%s", text)
                return
        if "/creator-micro/content/manage" in page.url:
            log.info("已跳转至抖音内容管理页：%s", page.url)
            jumped = True
            return

    # 未自动跳转：主动访问内容管理页核实最新作品标题
    if not jumped:
        try:
            page.goto("https://creator.douyin.com/creator-micro/content/manage", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            clean_title = re.sub(r"[【】《》#\s]", "", title)[:10]
            body = page.evaluate("() => document.body.innerText")
            if clean_title and clean_title in body:
                log.info("抖音内容管理页已核实出现新作品「%s」（发布成功）", clean_title)
                return
        except Exception:
            pass
        shot(page, "dy_publish_result_unknown")
        raise DouyinError(f"点击发布后未检测到成功标志（未跳转内容管理页且未在列表中找到新作品「{title[:12]}」）")


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

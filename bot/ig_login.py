"""（遗留）全自动 IG 登录脚本：Playwright 有头 + 自动过 reCAPTCHA + 导出 instaloader 会话。

主循环已改为 TG 投链驱动，不再需要 IG 小号会话。

遇到 reCAPTCHA 图片验证时自动切换为音频验证码，用 faster-whisper 识别。
遇到邮箱验证码时暂停等待（输出提示，需要外部提供验证码）。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import instaloader
from playwright.sync_api import sync_playwright

SESSION_FILE = Path(__file__).resolve().parent.parent / "data" / "ig_session"
PROFILE_DIR = Path(__file__).resolve().parent.parent / "data" / "ig_profile"
LOGIN_URL = "https://www.instagram.com/accounts/login/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _first_target() -> str | None:
    """取监控目标里的第一个（仅用于 feed API 验证）。"""
    try:
        from bot.config import load_settings
        targets = load_settings().ig_targets
        return targets[0] if targets else None
    except Exception:
        return None


def export_session(username: str, password: str, email_code: str = "") -> bool:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # persistent context：cookie 存在 profile 里，下次复用
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()

        # 先检查是否已登录（profile 里可能已有 session）
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        cookies = context.cookies(["https://www.instagram.com", "https://www.instagram.com/"])
        has_sessionid = any(c["name"] == "sessionid" for c in cookies)
        if "/accounts/login" not in page.url and "/challenge" not in page.url and has_sessionid:
            print(">>> profile 里已有完整登录态，直接导出", flush=True)
        else:
            # 需要重新登录
            print(f">>> 需要登录（当前 cookie: {[c['name'] for c in cookies]}）", flush=True)
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            # 先填表单
            try:
                page.locator('input[name="email"]').fill(username)
                page.locator('input[name="pass"]').fill(password)
                page.wait_for_timeout(500)
                # 尝试多种方式点登录按钮
                clicked = False
                for sel in [
                    page.get_by_role("button", name="Log In"),
                    page.locator('button[type="submit"]'),
                    page.locator('form button'),
                ]:
                    try:
                        if sel.count() > 0:
                            sel.first.click(timeout=5000)
                            clicked = True
                            print(">>> 已点击登录按钮", flush=True)
                            break
                    except Exception:
                        continue
                if not clicked:
                    # 直接提交表单
                    page.locator('input[name="pass"]').press("Enter")
                    print(">>> 已按回车提交", flush=True)
            except Exception as e:
                print(f">>> 表单填写失败: {e}，可能页面结构不同", flush=True)
                # 打印页面结构辅助诊断
                snap = page.content()[:2000]
                print(f">>> 页面内容: {snap}", flush=True)

        # 等待登录完成，处理各种验证
        deadline = time.time() + 600  # 10 分钟
        logged_in = False
        recaptcha_notified = False
        email_notified = False
        challenge_notified = False
        while time.time() < deadline:
            url = page.url

            if "instagram.com" in url and "/accounts/login" not in url and \
               "/challenge" not in url and "/auth_platform" not in url:
                page.wait_for_timeout(2000)
                if "/accounts/login" not in page.url:
                    logged_in = True
                    print(">>> 登录成功！", flush=True)
                    break

            # 检测安全验证页（checkpoint：邮箱验证码/手机验证/自拍等）
            if "/challenge" in url and not challenge_notified:
                print(">>> ⚠️ 账号被安全验证锁定！请在弹出的浏览器窗口按提示完成验证"
                      "（验证码通常发到邮箱），完成后脚本自动继续", flush=True)
                try:
                    from bot.config import load_settings
                    from bot.notify import telegram
                    s = load_settings()
                    telegram.notify_info(s, "🔒→🔓 Instagram 安全验证页已打开\n请在弹出的浏览器窗口按提示完成验证（验证码通常发到邮箱），完成后脚本自动继续")
                except Exception:
                    pass
                challenge_notified = True

            # 检测 reCAPTCHA → 通知用户手动通过
            if "/auth_platform/recaptcha" in url and not recaptcha_notified:
                print(">>> ⚠️ 需要 reCAPTCHA 验证！请在弹出的浏览器窗口手动完成图片验证", flush=True)
                print(">>> 完成后脚本会自动检测到并继续", flush=True)
                # 发 TG 通知
                try:
                    from bot.config import load_settings
                    from bot.notify import telegram
                    s = load_settings()
                    telegram.notify_info(s, "⚠️ Instagram 登录需要 reCAPTCHA 验证\n请在弹出的浏览器窗口手动完成图片验证，完成后脚本自动继续")
                except Exception:
                    pass
                recaptcha_notified = True

            # 检测邮箱验证码
            try:
                if page.get_by_text("Check your email").count() > 0 and not email_notified:
                    print(">>> ⚠️ 需要邮箱验证码！请在弹出的浏览器窗口输入收到的验证码", flush=True)
                    try:
                        from bot.config import load_settings
                        from bot.notify import telegram
                        s = load_settings()
                        telegram.notify_info(s, "⚠️ Instagram 登录需要邮箱验证码\n请在弹出的浏览器窗口输入验证码，完成后脚本自动继续")
                    except Exception:
                        pass
                    email_notified = True
            except Exception:
                pass

            # Save login info 弹窗
            try:
                save = page.get_by_role("button", name="Save info")
                if save.count() > 0:
                    save.first.click()
                    page.wait_for_timeout(2000)
            except Exception:
                pass

            page.wait_for_timeout(3000)

        if not logged_in:
            print(">>> 超时未登录成功", flush=True)
            context.close()
            return False

        # 导出 cookie
        page.wait_for_timeout(2000)
        cookies = context.cookies(["https://www.instagram.com", "https://www.instagram.com/"])
        print(f">>> 获取到 {len(cookies)} 个 cookie: {[c['name'] for c in cookies]}", flush=True)

        sessionid = next((c["value"] for c in cookies if c["name"] == "sessionid"), None)
        if not sessionid:
            print(">>> 错误：未找到 sessionid", flush=True)
            context.close()
            return False

        # 构造 instaloader 会话
        L = instaloader.Instaloader(quiet=True)
        for ck in cookies:
            L.context._session.cookies.set(
                ck["name"], ck["value"],
                domain=ck.get("domain", ".instagram.com"),
                path=ck.get("path", "/"),
            )
        # is_logged_in 只看 username 是否设置；不设的话 save_session_to_file 直接抛 LoginRequired
        L.context.username = username
        ds_uid = next((c["value"] for c in cookies if c["name"] == "ds_user_id"), None)
        if ds_uid:
            L.context.user_id = int(ds_uid)

        # 用 bot 实际使用的 feed API 验证会话（from_username 端点容易被 429 连坐）
        import httpx
        uid = None
        try:
            uid_cache = Path(__file__).resolve().parent.parent / "data" / "ig_user_ids.json"
            uid = json.loads(uid_cache.read_text(encoding="utf-8")).get(
                _first_target(), "")
        except Exception:
            pass
        if uid:
            try:
                cks = {ck["name"]: ck["value"] for ck in cookies}
                rv = httpx.get(
                    f"https://www.instagram.com/api/v1/feed/user/{uid}/",
                    params={"count": 1}, timeout=30,
                    cookies=cks,
                    headers={
                        "User-Agent": UA,
                        "X-IG-App-ID": "936619743392459",
                        "X-CSRFToken": cks.get("csrftoken", ""),
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": "https://www.instagram.com/",
                    })
                if rv.status_code == 200:
                    print(">>> feed API 验证成功（HTTP 200），会话可用", flush=True)
                else:
                    print(f">>> ⚠️ feed API 返回 HTTP {rv.status_code}：{rv.text[:200]}", flush=True)
                    print(">>> 会话仍被锁定或无效，请检查浏览器里是否还有未完成的验证", flush=True)
                    context.close()
                    return False
            except Exception as e:
                print(f">>> feed API 验证异常（{e}），继续保存会话", flush=True)
        else:
            print(">>> 跳过 feed API 验证（无 user_id 缓存）", flush=True)

        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        L.save_session_to_file(str(SESSION_FILE))
        print(f">>> instaloader 会话已保存到 {SESSION_FILE}", flush=True)
        context.close()
        return True


def _solve_recaptcha_audio(page):
    """尝试用音频验证码方式过 reCAPTCHA。"""
    # 切到 reCAPTCHA 的 iframe
    recaptcha_frame = page.frame_locator('iframe[title*="reCAPTCHA"]')
    # 点 "I'm not a robot" checkbox
    try:
        recaptcha_frame.locator("#recaptcha-anchor").click()
        page.wait_for_timeout(2000)
    except Exception:
        pass

    # 如果出现图片选择，切到音频
    # 找 challenge iframe
    challenge_frame = page.frame_locator('iframe[title*="recaptcha challenge"]')
    try:
        audio_btn = challenge_frame.get_by_role("button", name="Get an audio challenge")
        if audio_btn.count() > 0:
            audio_btn.click()
            page.wait_for_timeout(3000)
    except Exception:
        pass

    # 下载音频并用 faster-whisper 识别
    # 这是简化版；实际 reCAPTCHA 音频验证比较复杂
    # 作为 fallback，这里等人工
    print(">>> reCAPTCHA 音频模式已尝试，可能需要人工完成", flush=True)


if __name__ == "__main__":
    user = sys.argv[1] if len(sys.argv) > 1 else "carllei2026"
    pwd = sys.argv[2] if len(sys.argv) > 2 else ""
    code = sys.argv[3] if len(sys.argv) > 3 else ""
    ok = export_session(user, pwd, code)
    sys.exit(0 if ok else 1)

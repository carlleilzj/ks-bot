"""IG 遥控登录：VPS 无显示器环境下完成 IG 登录并导出 cookie。

为什么需要它
------------
IG 登录常触发 reCAPTCHA / 邮箱验证码 / 自拍验证。VPS 没有显示器，
所以在 Xvfb 虚拟屏里跑有头 Chromium（有头能规避部分自动化检测），
遇到验证时截图推送到 TG，用户回一条验证码，脚本填入后继续。

交互协议（TG 消息）
------------------
脚本 → 用户：
    🖼 [截图]
    🔐 IG 登录需要验证（useragent/reCAPTCHA/邮箱码）
    请回复验证码，或回复「继续」表示已在窗口内完成、点「跳过」

用户 → 脚本：
    6 位验证码 / 「继续」/ 「跳过」/ 「取消」

用法
----
    # Xvfb 里跑（推荐）
    xvfb-run -a .venv/bin/python -m bot.source.ig_login_remote \
        --add <username> --password <pwd>

    # 直接跑（若有 DISPLAY）
    .venv/bin/python -m bot.source.ig_login_remote --add <u> --password <p>
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from bot.source import ig_pool  # noqa: E402
from bot.source.ig_login_multi import export_cookies  # noqa: E402

PROFILE_ROOT = _ROOT / "data" / "ig_profiles"
SHOT_DIR = _ROOT / "data" / "ig_shots"
LOGIN_URL = "https://www.instagram.com/accounts/login/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _tg(s, text: str) -> None:
    try:
        from bot.notify import telegram
        telegram.send_text(s, text)
    except Exception as e:
        print(f"[TG 发送失败] {e}\n{text}", flush=True)


def _tg_photo(s, path: Path, caption: str = "") -> None:
    """发图到 TG。失败则退化为文字。"""
    token = getattr(s, "telegram_bot_token", "")
    chat = getattr(s, "telegram_chat_id", "")
    if not token or not chat:
        print(f"[无 TG 配置] 截图留在 {path}", flush=True)
        return
    base = getattr(s, "telegram_api_base", "https://api.telegram.org")
    url = f"{base}/bot{token}/sendPhoto"
    try:
        with open(path, "rb") as f:
            httpx.post(
                url,
                data={"chat_id": chat, "caption": caption[:1000]},
                files={"photo": ("shot.png", f, "image/png")},
                timeout=60,
            )
    except Exception as e:
        print(f"[TG 发图失败] {e}", flush=True)
        _tg(s, caption)


def _fetch_code_from_tg(s, timeout_sec: int = 240, after_id: int = 0) -> str:
    """轮询 TG 拿用户最新一条文本（验证码 / 继续 / 跳过 / 取消）。

    用 getUpdates 的 offset 前进策略：只取「本次提问之后」的消息。
    """
    token = getattr(s, "telegram_bot_token", "")
    chat = str(getattr(s, "telegram_chat_id", ""))
    if not token:
        return ""
    base = getattr(s, "telegram_api_base", "https://api.telegram.org")
    url = f"{base}/bot{token}/getUpdates"
    deadline = time.time() + timeout_sec

    # 先吃掉历史消息：拿到当前最新的 update_id 作为基线
    offset = after_id
    if not offset:
        try:
            with httpx.Client(timeout=20) as c:
                r = c.get(url, params={"timeout": 0, "limit": 100})
            ups = r.json().get("result", [])
            offset = (ups[-1]["update_id"] + 1) if ups else 0
        except Exception:
            offset = 0

    kw = getattr(s, "telegram_proxy", "") or ""
    kw_client = {"proxy": kw} if kw else {}
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=40, **kw_client) as c:
                r = c.get(url, params={"offset": offset, "timeout": 30})
            for up in r.json().get("result", []):
                offset = up["update_id"] + 1
                msg = up.get("message") or {}
                text = (msg.get("text") or "").strip()
                frm = str((msg.get("chat") or {}).get("id", ""))
                if chat and frm and frm != chat:
                    continue
                if text:
                    return text
        except Exception as e:
            print(f"[TG 轮询异常] {e}", flush=True)
            time.sleep(3)
    return ""


def _shot(page, tag: str) -> Path:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    p = SHOT_DIR / f"{tag}_{int(time.time())}.png"
    try:
        page.screenshot(path=str(p), full_page=False)
    except Exception as e:
        print(f"[截图失败] {e}", flush=True)
    return p


def login_remote(username: str, password: str, s,
                 timeout_sec: int = 1800) -> bool:
    """遥控登录。返回是否成功并导出 cookie。"""
    profile_dir = PROFILE_ROOT / username
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,          # Xvfb 下的「有头」，规避自动化检测
            user_agent=UA,
            viewport={"width": 1280, "height": 860},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=NetworkChangeNotifier,NetworkServiceChangeListener",
                "--no-sandbox",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            _tg(s, f"🚀 开始遥控登录 IG：{username}\n（脚本在 VPS 虚拟屏里跑，"
                   f"需要验证时我会发截图给你）")

            # 1. 已有登录态？
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded",
                      timeout=60000)
            page.wait_for_timeout(3500)
            cookies = context.cookies(["https://www.instagram.com"])
            if any(c["name"] == "sessionid" for c in cookies) and "/challenge" not in page.url:
                print(">>> profile 已有登录态", flush=True)
                export_cookies(context, username)
                _tg(s, f"✅ {username} profile 内已有登录态，已导出 cookie")
                return True

            # 2. 填表提交
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            for sel in ('input[name="email"]', 'input[name="username"]',
                        'input[aria-label*="username" i]'):
                loc = page.locator(sel)
                if loc.count():
                    loc.first.fill(username)
                    break
            pw = page.locator('input[name="pass"]')
            if not pw.count():
                pw = page.locator('input[type="password"]')
            if pw.count():
                pw.first.fill(password)
            clicked = False
            for sel in (page.get_by_role("button", name="Log in"),
                        page.get_by_role("button", name="登录"),
                        page.locator('button[type="submit"]')):
                try:
                    if sel.count():
                        sel.first.click(timeout=5000)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked and pw.count():
                pw.first.press("Enter")
            print(">>> 已提交登录表单", flush=True)

            # 3. 等待、必要时遥控
            deadline = time.time() + timeout_sec
            asked = set()
            while time.time() < deadline:
                url = page.url
                ok = ("instagram.com" in url
                      and "/accounts/login" not in url
                      and "/challenge" not in url
                      and "/auth_platform" not in url
                      and "/emailsignup" not in url)
                if ok:
                    page.wait_for_timeout(2500)
                    if "/accounts/login" not in page.url:
                        print(">>> 登录成功", flush=True)
                        export_cookies(context, username)
                        _tg(s, f"✅ IG {username} 登录成功，cookie 已导出")
                        return True

                # 需要人介入的场景
                need = None
                if "/challenge" in url:
                    need = "challenge"
                elif "/auth_platform" in url:
                    need = "recaptcha"
                else:
                    try:
                        if page.get_by_text("Check your email", exact=False).count():
                            need = "email"
                        elif page.get_by_text("Enter the code", exact=False).count():
                            need = "code"
                    except Exception:
                        pass

                if need and need not in asked:
                    asked.add(need)
                    shot = _shot(page, f"ig_{need}")
                    label = {
                        "challenge": "IG 安全验证（可能需要邮箱/短信验证码，或点「是我本人」）",
                        "recaptcha": "reCAPTCHA 图片验证",
                        "email": "邮箱验证码",
                        "code": "验证码输入",
                    }.get(need, need)
                    _tg_photo(s, shot, f"🔐 {username} 登录需要：{label}\n"
                                       f"请回复验证码；或回复「继续」让我重试；"
                                       f"回复「取消」放弃")
                    ans = _fetch_code_from_tg(s, timeout_sec=300)
                    if not ans:
                        print(">>> 未收到回复，继续等待页面变化", flush=True)
                        asked.discard(need)
                        continue
                    low = ans.strip().lower()
                    if low in ("取消", "cancel"):
                        _tg(s, "❌ 用户取消登录")
                        return False
                    if low in ("继续", "continue", "好了", "完成", "ok"):
                        print(">>> 用户表示已完成验证", flush=True)
                        page.wait_for_timeout(3000)
                        continue
                    # 当作验证码填入
                    print(f">>> 填入验证码：{ans}", flush=True)
                    filled = False
                    for sel in ('input[name="email"]', 'input[name="verificationCode"]',
                                'input[type="text"]', 'input[inputmode="numeric"]'):
                        loc = page.locator(sel)
                        for i in range(loc.count()):
                            try:
                                if loc.nth(i).is_visible():
                                    loc.nth(i).fill(ans)
                                    filled = True
                                    break
                            except Exception:
                                continue
                        if filled:
                            break
                    if filled:
                        for sel in (page.get_by_role("button", name="Next"),
                                    page.get_by_role("button", name="Confirm"),
                                    page.get_by_role("button", name="下一步"),
                                    page.locator('button[type="submit"]')):
                            try:
                                if sel.count():
                                    sel.first.click(timeout=4000)
                                    break
                            except Exception:
                                continue
                        _tg(s, f"✔️ 已提交验证码 {ans}，继续观察")
                    else:
                        _tg(s, "⚠️ 页面上没找到验证码输入框，请回复「继续」或「取消」")
                        asked.discard(need)

                page.wait_for_timeout(2500)

            _tg(s, f"⏱️ IG {username} 登录超时")
            return False
        finally:
            try:
                context.close()
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description="IG 遥控登录（Xvfb + TG 交互）")
    ap.add_argument("--add", metavar="USERNAME", required=True)
    ap.add_argument("--password", metavar="PWD", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    from bot.config import load_settings
    s = load_settings()

    ig_pool.add_account(args.add, args.password, args.note)
    ok = login_remote(args.add, args.password, s, args.timeout)
    if ok:
        accounts = ig_pool._load_accounts()
        for a in accounts:
            if a["username"] == args.add:
                a["use_count"] = 0
                a["blocked_until"] = 0
                a["last_error"] = ""
        ig_pool._save_accounts(accounts)
        print(">>> 成功", flush=True)
        return 0
    print(">>> 失败", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

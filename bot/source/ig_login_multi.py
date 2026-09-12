"""IG 多账号登录：Playwright 自动登录 + 导出 Netscape cookie 到账号池。

用法
----
    # 新增账号并登录（有头，能看到验证页）
    .venv/bin/python -m bot.source.ig_login_multi --add <username> --password <pwd>

    # 刷新所有账号 cookie
    .venv/bin/python -m bot.source.ig_login_multi --refresh-all

    # 查看池状态
    .venv/bin/python -m bot.source.ig_login_multi --status

设计
----
- 有头模式（headless=False）：IG 登录常需 reCAPTCHA / 邮箱验证码 / 自拍验证，
  必须有人能操作浏览器。脚本在验证页等待，完成后自动继续并导出 cookie。
- 每个账号独立 profile 目录，cookie 互不污染。
- 导出为 Netscape 格式（yt-dlp 可直接用），写进 data/cookies.d/<user>.txt。
- 同时写回账号池的 use_count=0 / blocked_until=0（新 cookie = 满血复活）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from bot.source import ig_pool  # noqa: E402

PROFILE_ROOT = _ROOT / "data" / "ig_profiles"
LOGIN_URL = "https://www.instagram.com/accounts/login/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _netscape_line(c: dict) -> str:
    """单条 cookie → Netscape 行。"""
    domain = c.get("domain", ".instagram.com")
    flag = "TRUE" if domain.startswith(".") else "FALSE"
    path = c.get("path", "/")
    secure = "TRUE" if c.get("secure", True) else "FALSE"
    expires = int(c.get("expires") or 0)
    if expires <= 0:
        expires = int(time.time()) + 400 * 86400
    name = c.get("name", "")
    value = c.get("value", "")
    return f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}"


def export_cookies(context, username: str) -> Path:
    """导出 IG 域下所有 cookie 到 data/cookies.d/<user>.txt。"""
    cookies = context.cookies(["https://www.instagram.com"])
    names = {c["name"] for c in cookies}
    if "sessionid" not in names:
        raise RuntimeError(f"导出失败：cookie 里没有 sessionid（拿到 {sorted(names)}）")

    ig_pool._COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    out = ig_pool._cookiefile_for(username)
    lines = [
        "# Netscape HTTP Cookie File",
        f"# 由 ig_login_multi 自动导出，账号：{username}",
        f"# 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for c in cookies:
        if "instagram.com" in c.get("domain", ""):
            lines.append(_netscape_line(c))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        out.chmod(0o600)
    except Exception:
        pass
    print(f">>> 已导出 {len(cookies)} 条 cookie → {out}", flush=True)
    return out


def login_account(username: str, password: str, timeout_sec: int = 900) -> bool:
    """有头登录一个账号并导出 cookie。返回是否成功。"""
    profile_dir = PROFILE_ROOT / username
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            user_agent=UA,
            viewport={"width": 1280, "height": 860},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            # 1. 先看 profile 里是否已有登录态
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded",
                      timeout=60000)
            page.wait_for_timeout(3000)
            cookies = context.cookies(["https://www.instagram.com"])
            has_session = any(c["name"] == "sessionid" for c in cookies)
            if has_session and "/accounts/login" not in page.url and "/challenge" not in page.url:
                print(">>> profile 内已有登录态，直接导出", flush=True)
                export_cookies(context, username)
                return True

            # 2. 打开登录页并填表
            print(f">>> 登录 {username} …", flush=True)
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            filled = False
            for sel in ('input[name="email"]', 'input[name="username"]',
                        'input[aria-label*="username" i]', 'input[aria-label*="手机" i]'):
                loc = page.locator(sel)
                if loc.count():
                    loc.first.fill(username)
                    filled = True
                    break
            pw = page.locator('input[name="pass"]')
            if not pw.count():
                pw = page.locator('input[type="password"]')
            if pw.count():
                pw.first.fill(password)
            if not filled or not pw.count():
                print(">>> !! 找不到登录表单，可能页面已改版。请手动在窗口里登录", flush=True)
            else:
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
                if not clicked:
                    pw.first.press("Enter")
                print(">>> 已提交登录表单，等待结果…", flush=True)

            # 3. 等待登录完成 / 验证
            deadline = time.time() + timeout_sec
            noted = set()
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
                        try:
                            export_cookies(context, username)
                        except Exception as e:
                            print(f">>> 导出失败：{e}", flush=True)
                            return False
                        return True

                if "/challenge" in url and "challenge" not in noted:
                    print(">>> ⚠️ 触发安全验证（checkpoint）！请在浏览器窗口完成验证"
                          "（验证码可能发到邮箱/手机），完成后脚本自动继续", flush=True)
                    noted.add("challenge")
                if "/auth_platform" in url and "recaptcha" not in noted:
                    print(">>> ⚠️ 需要 reCAPTCHA！请在浏览器窗口手动完成图片验证", flush=True)
                    noted.add("recaptcha")
                try:
                    if page.get_by_text("Check your email", exact=False).count() and "email" not in noted:
                        print(">>> ⚠️ 需要邮箱验证码！请查看邮箱并在窗口里输入", flush=True)
                        noted.add("email")
                except Exception:
                    pass

                page.wait_for_timeout(2500)

            print(">>> 超时未完成登录", flush=True)
            return False
        finally:
            try:
                context.close()
            except Exception:
                pass


def refresh_all(timeout_sec: int = 900) -> dict:
    """刷新所有账号的 cookie。返回 {username: 成功?}。"""
    results = {}
    for a in ig_pool.list_accounts():
        u, pw = a.get("username"), a.get("password")
        if not u or not pw:
            results[u or "?"] = False
            continue
        try:
            results[u] = login_account(u, pw, timeout_sec)
        except Exception as e:
            print(f">>> {u} 登录异常：{e}", flush=True)
            results[u] = False
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="IG 多账号登录与 cookie 导出")
    ap.add_argument("--add", metavar="USERNAME", help="新增账号并登录")
    ap.add_argument("--password", metavar="PWD", help="配合 --add 的密码")
    ap.add_argument("--note", default="", help="备注")
    ap.add_argument("--refresh", metavar="USERNAME", help="刷新指定账号")
    ap.add_argument("--refresh-all", action="store_true", help="刷新所有账号")
    ap.add_argument("--status", action="store_true", help="查看账号池状态")
    ap.add_argument("--timeout", type=int, default=900, help="登录等待秒数")
    args = ap.parse_args()

    if args.status:
        st = ig_pool.pool_status()
        print(f"账号池：{st['total']} 个，可用 {st['ready']} 个"
              f"{'（有遗留单 cookie 兜底）' if st['legacy_cookie'] else ''}")
        for a in st["accounts"]:
            print(f"  {a['username']:<24} {a['state']:<20} 已用 {a['use_count']:>4} 次"
                  f"  {a['last_error']}")
        return 0

    if args.add:
        if not args.password:
            print("!! --add 需要同时给 --password", file=sys.stderr)
            return 2
        ig_pool.add_account(args.add, args.password, args.note)
        ok = login_account(args.add, args.password, args.timeout)
        if ok:
            ig_pool.report_ok(args.add)  # 计数归 0 由这里近似（首次使用）
            accounts = ig_pool._load_accounts()
            for a in accounts:
                if a["username"] == args.add:
                    a["use_count"] = 0
                    a["blocked_until"] = 0
                    a["last_error"] = ""
            ig_pool._save_accounts(accounts)
        print(">>> 成功" if ok else ">>> 失败")
        return 0 if ok else 1

    if args.refresh:
        accounts = {a["username"]: a for a in ig_pool.list_accounts()}
        a = accounts.get(args.refresh)
        if not a:
            print(f"!! 账号池里没有 {args.refresh}", file=sys.stderr)
            return 2
        ok = login_account(a["username"], a["password"], args.timeout)
        print(">>> 成功" if ok else ">>> 失败")
        return 0 if ok else 1

    if args.refresh_all:
        res = refresh_all(args.timeout)
        for u, ok in res.items():
            print(f"  {u:<24} {'OK' if ok else 'FAIL'}")
        return 0 if res and all(res.values()) else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

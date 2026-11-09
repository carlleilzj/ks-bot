"""Instagram 素材发现：真实浏览器 + cookie，从账号主页抓作品链接。

为什么不能只用 httpx
-------------------
IG 的 API 现在校验 x-ig-www-claim / x-instagram-ajax / 浏览器指纹。
实测（2026-11-09）：
    - httpx + 正确 cookie  → web_profile_info 返回 429，topsearch 返回登录页 HTML
    - yt-dlp 的 instagram:user 提取器 → 被官方标记 broken，一律报「Unable to extract data」
    - Playwright 真实浏览器 + 同一份 cookie → **正常打开主页，DOM 里拿到 12 条 reel**

所以发现层必须走浏览器。cookie 只需提供登录态，指纹由浏览器自己生成。

为什么不解析 GraphQL 响应
-----------------------
IG 的 graphql 查询结构和 query_hash 会变，解析响应体极脆。
而**作品链接在 DOM 里**（a[href*="/reel/"]），这是稳定的公开结构。
拿链接就够了 —— 后续 extract_meta 会解析单条作品的完整信息。

cookie 来源
----------
data/cookies.d/*.txt（Netscape 格式）。只取 instagram.com 域的行。
2026-11-09 用户提供了两份浏览器全量导出，各自筛出 22 条 IG cookie，
其中 sessionid 都有效且属于两个不同账号（ds_user_id 不同）。

风控
----
浏览器访问比 API 温和，但仍有频率限制：
    - 每次只开一个账号、一个页面
    - 单次最多抓 limit 条
    - 失败不重试（重试只会加重风控），交给下一轮
"""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def cookies_from_netscape(path: Path) -> list[dict]:
    """读 Netscape 格式 cookie 文件，只保留 instagram.com 域。"""
    out: list[dict] = []
    try:
        text = Path(path).read_text(errors="ignore")
    except Exception as e:
        log.warning("读取 cookie 文件失败（%s）：%s", path, e)
        return []
    for line in text.splitlines():
        raw = line.removeprefix("#HttpOnly_")
        if not raw.strip() or raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 7 or "instagram" not in parts[0]:
            continue
        domain, _flag, cpath, secure, expires, name, value = parts[:7]
        try:
            exp = float(expires)
        except ValueError:
            exp = -1
        out.append({
            "name": name,
            "value": value,
            "domain": domain,
            "path": cpath or "/",
            "expires": exp if exp > 0 else -1,
            "httpOnly": line.startswith("#HttpOnly_"),
            "secure": str(secure).upper() == "TRUE",
            "sameSite": "Lax",
        })
    return out


def pick_cookiefile(cookie_dir: Path) -> Path | None:
    """挑一个含 sessionid 的 IG cookie 文件。"""
    if not cookie_dir.exists():
        return None
    candidates = []
    for f in sorted(cookie_dir.glob("*.txt")):
        if f.name.endswith(".bak"):
            continue
        cks = cookies_from_netscape(f)
        if any(c["name"] == "sessionid" and c["value"].strip() for c in cks):
            candidates.append(f)
    if not candidates:
        return None
    return random.choice(candidates)


def discover_instagram_profile(username: str, limit: int = 12,
                               cookiefile: Path | None = None,
                               cookie_dir: Path | None = None) -> list[dict]:
    """打开账号主页，从 DOM 取作品链接。

    返回 [{"url": "https://www.instagram.com/reel/XXX/", "code": "XXX"}]，
    失败返回空列表（不抛异常，发现层失败不应影响发布主流程）。
    """
    from playwright.sync_api import sync_playwright

    if cookiefile is None:
        cookie_dir = cookie_dir or Path("data/cookies.d")
        cookiefile = pick_cookiefile(cookie_dir)
    if not cookiefile:
        log.debug("IG 发现跳过：没有含 sessionid 的 cookie 文件")
        return []

    cks = cookies_from_netscape(cookiefile)
    if not cks:
        return []

    handle = username.lstrip("@").strip("/")
    found: list[dict] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=_UA,
                locale="en-US",
            )
            try:
                ctx.add_cookies(cks)
                page = ctx.new_page()
                page.goto(f"https://www.instagram.com/{handle}/",
                          wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(7000)

                links = page.evaluate("""() => {
                    const out = [];
                    document.querySelectorAll('a[href*="/reel/"], a[href*="/p/"]')
                      .forEach(a => {
                        const h = a.getAttribute('href') || '';
                        if (h && !out.includes(h)) out.push(h);
                      });
                    return out;
                }""")
                for href in (links or []):
                    if len(found) >= limit:
                        break
                    # /harun_home/reel/XXX/ 或 /reel/XXX/
                    parts = [p for p in href.split("/") if p]
                    if "reel" in parts:
                        code = parts[parts.index("reel") + 1]
                        found.append({
                            "url": f"https://www.instagram.com/reel/{code}/",
                            "code": code,
                        })
                    elif "p" in parts:
                        code = parts[parts.index("p") + 1]
                        found.append({
                            "url": f"https://www.instagram.com/p/{code}/",
                            "code": code,
                        })
            finally:
                browser.close()
    except Exception as e:
        log.warning("IG 主页抓取失败（%s）：%s", handle, str(e)[:140])
        return []

    log.info("IG 发现：@%s 取到 %d 条作品链接", handle, len(found))
    return found

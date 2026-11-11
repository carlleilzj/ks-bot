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
from urllib.parse import quote

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


_bad_cookies: dict[str, float] = {}   # cookie path -> cooled until timestamp
_COOKIE_COOLDOWN_SEC = 1800            # 被 IG 登录墙拦截后冷却 30 分钟


def _is_login_wall(url: str) -> bool:
    u = (url or "").strip().rstrip("/")
    return (
        "/accounts/login" in u
        or "/challenge" in u
        or u in ("https://www.instagram.com", "http://www.instagram.com")
    )


def pick_cookiefile(cookie_dir: Path) -> Path | None:
    """挑一个含有效 sessionid 且未处于冷却期的 IG cookie 文件。"""
    if not cookie_dir.exists():
        return None
    now = time.time()
    candidates = []
    for f in sorted(cookie_dir.glob("*.txt")):
        if f.name.endswith(".bak"):
            continue
        if _bad_cookies.get(str(f), 0) > now:
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

    2026-11-11 实测优化：
    - 公开账号的 `/{handle}/reels/` 页无需登录态即可直接拿到 12 条 Reel 链接！
    - 优先用【无 Cookie 匿名浏览器】抓取白名单账号，彻底避免把有限的小号 sessionid
      烧在白名单巡检上；仅当匿名失败（或显式传入 cookiefile）时再试登录态。
    """
    handle = username.lstrip("@").strip("/")
    reels_path = f"https://www.instagram.com/{handle}/reels/"
    # 1. 优先匿名抓取 /reels/（零账号损耗）
    if cookiefile is None:
        found = _scrape_links(reels_path, limit, None, cookie_dir,
                              label=f"@{handle}/reels", allow_anonymous=True,
                              prefer_anonymous=True)
        if found:
            return found

    # 2. 匿名未拿到（可能页面限制）或显式传了 cookiefile → 试登录态
    found = _scrape_links(reels_path, limit, cookiefile, cookie_dir,
                          label=f"@{handle}/reels", allow_anonymous=True)
    if found:
        return found

    path = f"https://www.instagram.com/{handle}/"
    return _scrape_links(path, limit, cookiefile, cookie_dir,
                         label=f"@{handle}", allow_anonymous=True,
                         prefer_anonymous=(cookiefile is None))


def discover_instagram_tag(tag: str, limit: int = 24,
                           cookiefile: Path | None = None,
                           cookie_dir: Path | None = None,
                           pages: int = 1) -> list[dict]:
    """打开 hashtag 页，取该话题下的帖子链接。

    2026-11-09 实测可用：explore/tags/bedroomdecor 与中文 #床品 都能拿到
    24 条链接。这才是「关键词搜索」的正确形态 —— 之前我误判成不可用，
    是因为只测了 API 接口（429/404），没测网页。

    pages > 1 时滚动触发翻页，拿更多候选。
    为什么要翻页：一屏只有 24 条，单标签跑几轮就耗尽。
    注意 IG 的 hashtag 页**没有时间排序参数**（实测 GraphQL variables 只有
    query/after/first 三个），是推荐排序，所以翻页拿到的不是「更新」而是
    「更多」，对带货选品反而合适。
    """
    t = tag.lstrip("#").strip().replace(" ", "")
    path = f"https://www.instagram.com/explore/tags/{quote(t)}/"
    return _scrape_links(path, limit, cookiefile, cookie_dir,
                         label=f"#{t}", pages=pages)


def discover_instagram_keyword(query: str, limit: int = 24,
                               cookiefile: Path | None = None,
                               cookie_dir: Path | None = None) -> list[dict]:
    """打开关键词搜索结果页。

    与 hashtag 的区别：hashtag 是「有人主动打了这个标签」，
    关键词搜索是「平台按内容匹配」。实测两者返回的集合不同
    （keyword 页首条与 hashtag 页不重合），可以互补。
    """
    q = quote(query.strip())
    path = f"https://www.instagram.com/explore/search/keyword/?q={q}"
    return _scrape_links(path, limit, cookiefile, cookie_dir,
                         label=f"kw:{query}")


def _scrape_links(path: str, limit: int,
                  cookiefile: Path | None,
                  cookie_dir: Path | None,
                  label: str = "",
                  pages: int = 1,
                  allow_anonymous: bool = False,
                  prefer_anonymous: bool = False) -> list[dict]:
    """打开 path，从 DOM 取 /reel/ 与 /p/ 链接。三个入口共用。

    pages > 1 时反复滚动页面底部触发懒加载，再重新采集。
    每滚动一次等待 2.5 秒 —— 太短拿不到新内容，太长浪费时间。
    """
    from playwright.sync_api import sync_playwright

    cks: list[dict] = []
    used_cookiefile: Path | None = None
    if not prefer_anonymous:
        if cookiefile is None:
            cookie_dir = cookie_dir or Path("data/cookies.d")
            cookiefile = pick_cookiefile(cookie_dir)
        if cookiefile:
            cks = cookies_from_netscape(cookiefile)
            used_cookiefile = cookiefile
        elif not allow_anonymous:
            log.debug("IG 发现跳过（%s）：没有含有效 sessionid 的 cookie 文件", label or path)
            return []

    found: list[dict] = []
    seen_codes: set[str] = set()

    def _collect(page) -> int:
        """采集一次，返回本次新增条数。"""
        links = page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('a[href*="/reel/"], a[href*="/p/"]')
              .forEach(a => {
                const h = a.getAttribute('href') || '';
                if (h && !out.includes(h)) out.push(h);
              });
            return out;
        }""")
        added = 0
        # 优先收录 /reel/ 短视频链接（100% 为可下载视频），再收录 /p/（混合图文）
        sorted_links = sorted(links or [], key=lambda h: 0 if "/reel/" in h else 1)
        for href in sorted_links:
            if len(found) >= limit:
                break
            parts = [p for p in href.split("/") if p]
            code = None
            if "reel" in parts:
                code = parts[parts.index("reel") + 1]
                url = f"https://www.instagram.com/reel/{code}/"
            elif "p" in parts:
                code = parts[parts.index("p") + 1]
                url = f"https://www.instagram.com/p/{code}/"
            else:
                continue
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            found.append({"url": url, "code": code})
            added += 1
        return added

    # 页面偶发加载超时（实测 #四件套 一次 0 条、重跑 24 条），
    # 单次失败不该让整个标签作废 —— 给一次重试，仍失败才放弃。
    for attempt in range(1, 3):
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=_UA,
                    locale="en-US",
                )
                try:
                    if cks:
                        ctx.add_cookies(cks)
                    page = ctx.new_page()
                    page.goto(path, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(7000)

                    # 若带 Cookie 却被 IG 强制跳到了登录墙，说明 Cookie 已失效/被风控，立即熔断该 Cookie
                    if _is_login_wall(page.url):
                        if used_cookiefile:
                            _bad_cookies[str(used_cookiefile)] = time.time() + _COOKIE_COOLDOWN_SEC
                            log.warning("IG 抓取 %s 遭遇登录墙（%s），Cookie %s 已暂停 30 分钟",
                                        label or path, page.url, used_cookiefile.name)
                            cks = []
                            used_cookiefile = None
                            if allow_anonymous:
                                # 立刻切匿名兜底再试一次
                                ctx.clear_cookies()
                                page.goto(path, wait_until="domcontentloaded", timeout=45000)
                                page.wait_for_timeout(5000)
                            else:
                                return found
                        else:
                            log.debug("IG 抓取 %s 需要登录墙（%s），跳过", label or path, page.url)
                            return found

                    _collect(page)

                    for _ in range(max(0, pages - 1)):
                        if len(found) >= limit:
                            break
                        try:
                            page.evaluate(
                                "window.scrollTo(0, document.body.scrollHeight)")
                            page.wait_for_timeout(2500)
                        except Exception:
                            break
                        if _collect(page) == 0:
                            break
                finally:
                    browser.close()
            if found or attempt == 2:
                break
            log.info("IG 抓取 %s 首轮空结果，重试一次", label or path)
        except Exception as e:
            if attempt == 2:
                log.warning("IG 页面抓取失败（%s）：%s",
                            label or path, str(e)[:140])
                return found
            log.debug("IG 抓取 %s 第 %d 次异常，重试：%s",
                      label or path, attempt, str(e)[:100])

    log.info("IG 发现：%s 取到 %d 条作品链接", label or path, len(found))
    return found

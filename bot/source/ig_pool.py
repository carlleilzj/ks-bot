"""IG 账号池：多账号 cookie 轮换 + 冷却，规避单账号风控。

背景
----
单账号从固定 VPS IP 高频拉取 IG，必然触发 checkpoint_required / HTTP 400。
本模块维护一个账号池，按需轮换 cookie；某账号被风控时单独冷却，
其余账号接管，避免整条 IG 链路瘫痪。

文件布局
--------
    data/ig_accounts.json          账号池（明文凭据，chmod 600）
    data/cookies.d/<user>.txt      各账号导出的 cookie（Netscape 格式）
    data/ig_profiles/<user>/       Playwright 持久化 profile（含登录态）

配置（config.yaml）
------------------
    instagram:
      enabled: true
      rotate_every: 20         # 每账号连续使用多少次后轮换
      cooldown_min: 360        # 被风控后的冷却时长（分钟）

对外接口
--------
    pick_cookiefile()  -> Path | None    取当前可用账号的 cookie 文件
    report_ok(user)                       标记成功（计数 +1）
    report_blocked(user, reason)          标记被风控（进入冷却）
    pool_status()     -> dict             池状态（诊断用）
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_ACCOUNTS_FILE = _ROOT / "data" / "ig_accounts.json"
_COOKIE_DIR = _ROOT / "data" / "cookies.d"
_LEGACY_COOKIE = _ROOT / "data" / "cookies.txt"

# 被判定为风控的错误特征（命中即冷却该账号）
_BLOCK_MARKERS = (
    "checkpoint_required",
    "useragent mismatch",
    "HTTP Error 400",
    "HTTP Error 429",
    "login_required",
    "Please wait a few minutes",
    "rate limit",
)

DEFAULT_ROTATE_EVERY = 20
DEFAULT_COOLDOWN_MIN = 360


def _load_accounts() -> list[dict]:
    """读账号池。文件不存在时返回空列表。"""
    if not _ACCOUNTS_FILE.exists():
        return []
    try:
        data = json.loads(_ACCOUNTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning("账号池文件解析失败：%s", e)
        return []


def _save_accounts(accounts: list[dict]) -> None:
    _ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _ACCOUNTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _ACCOUNTS_FILE)
    try:
        _ACCOUNTS_FILE.chmod(0o600)
    except Exception:
        pass


def _cookiefile_for(username: str) -> Path:
    return _COOKIE_DIR / f"{username}.txt"


def is_blocked(err: str) -> bool:
    """错误文本是否属于风控特征。"""
    low = (err or "").lower()
    return any(m.lower() in low for m in _BLOCK_MARKERS)


# ---------- 账号池维护 ----------

def add_account(username: str, password: str, note: str = "") -> None:
    """加账号（已存在则更新密码）。"""
    accounts = _load_accounts()
    for a in accounts:
        if a.get("username") == username:
            a["password"] = password
            if note:
                a["note"] = note
            a["enabled"] = True
            _save_accounts(accounts)
            log.info("账号池已更新：%s", username)
            return
    accounts.append({
        "username": username,
        "password": password,
        "note": note,
        "enabled": True,
        "use_count": 0,
        "blocked_until": 0,
        "last_error": "",
        "last_ok_at": 0,
        "created_at": time.time(),
    })
    _save_accounts(accounts)
    log.info("账号池已新增：%s", username)


def list_accounts() -> list[dict]:
    return _load_accounts()


def remove_account(username: str) -> bool:
    accounts = _load_accounts()
    n = len(accounts)
    accounts = [a for a in accounts if a.get("username") != username]
    if len(accounts) == n:
        return False
    _save_accounts(accounts)
    return True


# ---------- 选择与轮换 ----------

def _has_sessionid(cf: Path) -> bool:
    """cookie 文件是否含 IG 的登录凭据 sessionid。

    2026-09-27 事故：data/cookies.d/carllei2026.txt 只有匿名 cookie
    （datr/ig_did/mid/csrftoken…），没有 sessionid，池里却照样选中它 ——
    结果是自研 IG 解析必然失败、回退 yt-dlp 兜底。选号时必须排除这种
    「看着有文件、实际没登录」的账号。
    """
    try:
        with cf.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 7 and parts[5] == "sessionid" and parts[6].strip():
                    return True
    except Exception:
        return False
    return False


def pick_cookiefile(rotate_every: int = DEFAULT_ROTATE_EVERY) -> Path | None:
    """选一个可用账号的 cookie 文件。

    规则：
      1. 过滤掉 disabled / 冷却中的账号
      2. **优先带 sessionid 的账号**（自研解析器需要登录态；只有匿名 cookie
         的账号只能走 yt-dlp 兜底，属降级）
      3. 同级内按 use_count 升序（负载均衡），达到 rotate_every 的让位
      4. 池内无可用账号时，才扫描 cookie 目录；扫描同样排除已禁用的账号
      5. 最后回退遗留单 cookie 文件

    2026-09-27 事故：data/cookies.d/carllei2026.txt 只有匿名 cookie
    （datr/ig_did/mid/csrftoken…），没有 sessionid，池里却照样优先选中它 ——
    自研 IG 解析必然失败。但注意**不能因此直接踢掉它**：实测它在 yt-dlp
    路径下仍能抓到公开 Reel。正确做法是让它降级、排在登录态账号之后。
    """
    now = time.time()
    accounts = _load_accounts()

    # 显式禁用的账号（如被 IG 限制的主号）任何路径都不得选中
    disabled = {a.get("username") for a in accounts if not a.get("enabled", True)}

    logged_in: list[tuple[dict, Path]] = []
    anonymous: list[tuple[dict, Path]] = []
    for a in accounts:
        if not a.get("enabled", True):
            continue
        if a.get("blocked_until", 0) > now:
            continue
        cf = _cookiefile_for(a["username"])
        if not cf.exists() or cf.stat().st_size == 0:
            continue
        if _has_sessionid(cf):
            logged_in.append((a, cf))
        else:
            log.info("IG 账号 %s 的 cookie 缺 sessionid，降级为匿名（yt-dlp 兜底）",
                     a["username"])
            anonymous.append((a, cf))

    def _order(pairs: list[tuple[dict, Path]]) -> list[tuple[dict, Path]]:
        return sorted(pairs, key=lambda t: (t[0].get("use_count", 0) >= rotate_every,
                                            t[0].get("use_count", 0)))

    for group in (logged_in, anonymous):
        if group:
            acct, cf = _order(group)[0]
            log.debug("IG 账号池选中：%s（已用 %d 次，登录态=%s）",
                      acct["username"], acct.get("use_count", 0),
                      "是" if group is logged_in else "否")
            return cf

    # 池内无可用账号：扫目录，但排除已禁用的用户名
    for cf in sorted(_COOKIE_DIR.glob("*.txt")):
        if cf.stem in disabled:
            continue
        if cf.stat().st_size > 0 and _has_sessionid(cf):
            log.info("账号池无可用账号，兜底选用带 sessionid 的 cookie：%s", cf.name)
            return cf

    # 兜底：遗留单 cookie
    if _LEGACY_COOKIE.exists() and _LEGACY_COOKIE.stat().st_size > 0:
        return _LEGACY_COOKIE
    return None


def account_of(cookiefile: Path | str | None) -> str:
    """从 cookie 文件路径反推账号名（遗留文件返回空串）。"""
    if not cookiefile:
        return ""
    p = Path(cookiefile)
    if p.parent == _COOKIE_DIR:
        return p.stem
    return ""


def report_ok(username: str) -> None:
    """记一次成功使用。"""
    if not username:
        return
    accounts = _load_accounts()
    for a in accounts:
        if a.get("username") == username:
            a["use_count"] = a.get("use_count", 0) + 1
            a["last_ok_at"] = time.time()
            a["last_error"] = ""
            a["blocked_until"] = 0
            _save_accounts(accounts)
            return


def report_blocked(username: str, reason: str,
                   cooldown_min: int = DEFAULT_COOLDOWN_MIN) -> None:
    """标记账号被风控，进入冷却。"""
    if not username:
        return
    accounts = _load_accounts()
    for a in accounts:
        if a.get("username") == username:
            a["blocked_until"] = time.time() + cooldown_min * 60
            a["last_error"] = (reason or "")[:200]
            _save_accounts(accounts)
            log.warning("IG 账号 %s 进入冷却 %d 分钟：%s",
                        username, cooldown_min, (reason or "")[:100])
            return


def pool_status() -> dict:
    """池状态快照（诊断/看板用）。"""
    now = time.time()
    accounts = _load_accounts()
    out = []
    for a in accounts:
        cf = _cookiefile_for(a["username"])
        blocked_until = a.get("blocked_until", 0)
        if not a.get("enabled", True):
            state = "disabled"
        elif blocked_until > now:
            state = f"cooling({int((blocked_until - now) / 60)}min)"
        elif not cf.exists():
            state = "no_cookie"
        else:
            state = "ready"
        out.append({
            "username": a["username"],
            "state": state,
            "use_count": a.get("use_count", 0),
            "last_error": (a.get("last_error") or "")[:80],
        })
    return {
        "total": len(accounts),
        "ready": sum(1 for x in out if x["state"] == "ready"),
        "accounts": out,
        "legacy_cookie": _LEGACY_COOKIE.exists(),
    }

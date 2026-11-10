"""源账号白名单：人工推荐 + 数据验证的高质量素材来源。

为什么需要
----------
自动发现靠关键词搜索，覆盖面广但质量参差。已知的优质源账号（B 号的
هارون هوم 中位 3206、GRAND STYLE 2622 等）目前只存在于历史数据的
username 字段里，用完就散，没有沉淀。

本模块把「源账号」变成一等公民：
    data/source_accounts.json   白名单（平台 → 赛道 → 账号列表）

白名单账号的用途
--------------
1. 发现层优先从这些账号的 feed 拉新内容（质量可控）
2. 关键词搜索作为补充（覆盖面）

TG 交互（用户手动加）
-------------------
把账号主页链接直接发给 bot：
    https://www.instagram.com/harun_home/
    https://www.youtube.com/@HomeDecor
    或者纯用户名：@harun_home
自动识别平台与账号名，归入对应赛道的白名单。

为什么不用「自动打分选白名单」
--------------------------
2026-11-09 实测：B 号 43 个源账号里 35 个只有 1 条样本，只有 5 个 >=3 条。
样本这么少时，中位数会被单条爆款/单条哑弹完全左右
（Lolly Pop 靠一条 14 万播放排到第一，而 n=8 的 هارون هوم 被判偏低）。
所以白名单以**人工判断 + 数据参考**为主，不自动增删。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
ACCOUNTS_FILE = _ROOT / "data" / "source_accounts.json"


def _load() -> dict:
    if not ACCOUNTS_FILE.exists():
        return {}
    try:
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning("源账号白名单解析失败：%s", e)
        return {}


def _save(data: dict) -> None:
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, ACCOUNTS_FILE)
    try:
        ACCOUNTS_FILE.chmod(0o644)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 解析用户发来的链接/用户名
# ---------------------------------------------------------------------------

_IG_RES = [
    re.compile(r"instagram\.com/([A-Za-z0-9_.]+)/?", re.I),
    re.compile(r"instagram\.com/([A-Za-z0-9_.]+)/(?:reel|p)/", re.I),
]
_YT_HANDLE_RE = re.compile(r"youtube\.com/@([A-Za-z0-9_.\-]+)", re.I)
_YT_CHANNEL_RE = re.compile(r"youtube\.com/channel/([A-Za-z0-9_\-]+)", re.I)
_YT_C_RE = re.compile(r"youtube\.com/c/([A-Za-z0-9_.\-]+)", re.I)
_PLAIN_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_.\-]{2,30})$")

# Instagram 的保留路径，不是用户名的
_IG_RESERVED = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts",
    "direct", "about", "legal", "developer", "directory", "web",
}


def parse_account_ref(text: str) -> tuple[str, str, str] | None:
    """从用户输入里解析出 (platform, account, canonical_url)。

    支持：
        https://www.instagram.com/harun_home/
        https://instagram.com/reel/XXX/           → 归到该 reel 的作者（需另查，这里只返回空）
        https://www.youtube.com/@HomeDecor
        https://www.youtube.com/channel/UCxxxx
        @harun_home                               → 平台未知，需要用户指定

    返回 None 表示不是账号引用。
    """
    t = (text or "").strip()
    if not t:
        return None

    low = t.lower()

    if "instagram.com" in low:
        for rx in _IG_RES:
            m = rx.search(t)
            if m:
                name = m.group(1).strip("/")
                if name.lower() in _IG_RESERVED:
                    return None
                return ("instagram", name,
                        f"https://www.instagram.com/{name}/")

    if "youtube.com" in low or "youtu.be" in low:
        m = _YT_HANDLE_RE.search(t)
        if m:
            h = m.group(1)
            return ("youtube", h, f"https://www.youtube.com/@{h}")
        m = _YT_CHANNEL_RE.search(t)
        if m:
            cid = m.group(1)
            return ("youtube", cid, f"https://www.youtube.com/channel/{cid}")
        m = _YT_C_RE.search(t)
        if m:
            h = m.group(1)
            return ("youtube", h, f"https://www.youtube.com/c/{h}")
        return None

    # 纯用户名 / @handle：平台由调用方指定
    m = _PLAIN_HANDLE_RE.match(t)
    if m:
        return ("", m.group(1), "")

    return None


# ---------------------------------------------------------------------------
# 白名单读写
# ---------------------------------------------------------------------------

def list_accounts(lane: str | None = None) -> dict:
    """读取白名单。lane 为空返回全部；否则只返回该赛道。"""
    data = _load()
    if lane:
        return data.get(lane) or {}
    return data


def accounts_for(lane: str, platform: str) -> list[dict]:
    """取某赛道某平台的白名单账号。"""
    return list((list_accounts(lane).get(platform) or []))


def add_account(lane: str, platform: str, account: str,
                url: str = "", note: str = "") -> tuple[bool, str]:
    """加一个白名单账号。返回 (是否新增, 说明)。

    幂等：已存在只更新 url/note，不重复添加。
    """
    if not lane or not platform or not account:
        return False, "缺少 lane/platform/account"
    data = _load()
    lane_obj = data.setdefault(lane, {})
    items = lane_obj.setdefault(platform, [])
    for it in items:
        if str(it.get("account", "")).lower() == account.lower():
            changed = False
            if url and it.get("url") != url:
                it["url"] = url
                changed = True
            if note and it.get("note") != note:
                it["note"] = note
                changed = True
            if changed:
                _save(data)
            return False, f"已存在（{'已更新' if changed else '无变化'}）"
    items.append({
        "account": account,
        "url": url or "",
        "note": note or "",
        "added_at": _now(),
    })
    _save(data)
    log.info("源账号白名单新增：%s/%s/%s", lane, platform, account)
    return True, "已添加"


def remove_account(lane: str, platform: str, account: str) -> bool:
    data = _load()
    items = (data.get(lane) or {}).get(platform) or []
    keep = [i for i in items
            if str(i.get("account", "")).lower() != account.lower()]
    if len(keep) == len(items):
        return False
    data[lane][platform] = keep
    _save(data)
    return True


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 预置：从历史数据里筛出的高置信账号
# ---------------------------------------------------------------------------

# 判定依据（2026-11-09，B 号 63 条已发布）：
#   n >= 2 且 中位播放 >= 2000 且 题材确为家居
# 刻意排除：
#   - n=1 的账号（35 个）：单条样本无法区分「账号好」和「这条运气好」
#   - 泛内容号（Hannah Bentley / Lena / maya.nicole6776 等）：题材不垂直
#   - Twistana Pyjama Maroc：n=2 但中位仅 236
#
# ⚠️ 只预置「用户名能在库里直接读到」的账号。
# 显示名是 emoji/阿拉伯语的那些（𝐄𝐋𝐋𝐀𝐇 | 𝐇𝐎𝐌𝐄 等），Instagram 的
# 实际用户名与显示名不同，猜一个会拉到错的账号 —— 这类靠用户手动发链接补。
SEED_HOME = [
    {"account": "harun_home", "url": "https://www.instagram.com/harun_home/",
     "note": "n=8 中位 3206（库内显示名 هارون هوم）"},
    {"account": "cilinea", "url": "https://www.instagram.com/cilinea/",
     "note": "n=3 中位 4104"},
    {"account": "grand.style", "url": "https://www.instagram.com/grand.style/",
     "note": "n=6 中位 2622（库内显示名 GRAND STYLE，用户名待用户核对）"},
]

# 快手 A 号爆款源账号（2026-11-10 从历史 Top 爆款实测数据中精准反查）
# 定位：以小动物/奇幻生物为主角的 AI 3D 搞笑剧情动画微剧场
SEED_ANIMALS = [
    {"account": "kataicinema", "url": "https://www.instagram.com/kataicinema/",
     "note": "Top1 383万大爆款源头（Paco 变色龙 AI 动画）"},
    {"account": "chipcoi888", "url": "https://www.instagram.com/chipcoi888/",
     "note": "Top2 134万大爆款源头（拟人蜜獾平头哥短片）"},
    {"account": "mamahentale", "url": "https://www.instagram.com/mamahentale/",
     "note": "Top3 12.5万大爆款源头（母鸡历险记 3D 动画）"},
    {"account": "bingwave123", "url": "https://www.instagram.com/bingwave123/",
     "note": "Top4 11.4万大爆款源头（小鸟与鱼无声 3D 动画）"},
    {"account": "aivoxy", "url": "https://www.instagram.com/aivoxy/",
     "note": "1.7万播放（树洞小动物治愈动画）"},
    {"account": "cart.oons9493", "url": "https://www.instagram.com/cart.oons9493/",
     "note": "1.1万播放（小鸡搞笑动画短片）"},
    {"account": "cartoonworldtop", "url": "https://www.instagram.com/cartoonworldtop/",
     "note": "1.1万播放（母鸡孵出巨龙奇幻动画）"},
    {"account": "zooya.world", "url": "https://www.instagram.com/zooya.world/",
     "note": "8267播放（毛茸茸小家伙奇妙日常）"},
    {"account": "hello_bug88", "url": "https://www.instagram.com/hello_bug88/",
     "note": "6747播放（火山翻车搞笑动画）"},
    {"account": "she_talks_ai", "url": "https://www.instagram.com/she_talks_ai/",
     "note": "4015播放（Bruno & Luma 3D 系列动画）"},
]


# 海外 4 个矩阵号对应的预置白名单
SEED_OVERSEAS = [
    # ig_pets 萌宠
    {"lane": "ig_pets", "account": "kataicinema", "url": "https://www.instagram.com/kataicinema/", "note": "3D动物搞笑动画"},
    {"lane": "ig_pets", "account": "mamahentale", "url": "https://www.instagram.com/mamahentale/", "note": "萌宠母鸡历险"},
    {"lane": "ig_pets", "account": "bingwave123", "url": "https://www.instagram.com/bingwave123/", "note": "动物无声动画"},
    # ig_anim 动画
    {"lane": "ig_anim", "account": "aivoxy", "url": "https://www.instagram.com/aivoxy/", "note": "治愈3D动画"},
    {"lane": "ig_anim", "account": "she_talks_ai", "url": "https://www.instagram.com/she_talks_ai/", "note": "3D脑洞短片"},
    {"lane": "ig_anim", "account": "cartoonworldtop", "url": "https://www.instagram.com/cartoonworldtop/", "note": "奇幻小剧场"},
    # ig_home 家居
    {"lane": "ig_home", "account": "harun_home", "url": "https://www.instagram.com/harun_home/", "note": "家纺四件套实拍"},
    {"lane": "ig_home", "account": "cilinea", "url": "https://www.instagram.com/cilinea/", "note": "软装与睡衣穿搭"},
    {"lane": "ig_home", "account": "grand.style", "url": "https://www.instagram.com/grand.style/", "note": "轻奢卧室质感"},
]


def seed_if_empty() -> int:
    """首次运行时写入预置白名单（已有内容则不动）。返回写入条数。"""
    if ACCOUNTS_FILE.exists() and _load():
        return 0
    n = 0
    for item in SEED_HOME:
        ok, _ = add_account("kuaishou2", "instagram",
                            item["account"], item["url"], item["note"])
        n += int(ok)
    for item in SEED_ANIMALS:
        ok, _ = add_account("kuaishou", "instagram",
                            item["account"], item["url"], item["note"])
        n += int(ok)
    for item in SEED_OVERSEAS:
        ok, _ = add_account(item["lane"], "instagram",
                            item["account"], item["url"], item["note"])
        n += int(ok)
    return n


def sync_builtin_seeds() -> int:
    """将内置的爆款高置信账号补入白名单（已有不重复添加，幂等）。返回新补充条数。"""
    n = 0
    for item in SEED_HOME:
        ok, _ = add_account("kuaishou2", "instagram",
                            item["account"], item["url"], item["note"])
        n += int(ok)
    for item in SEED_ANIMALS:
        ok, _ = add_account("kuaishou", "instagram",
                            item["account"], item["url"], item["note"])
        n += int(ok)
    for item in SEED_OVERSEAS:
        ok, _ = add_account(item["lane"], "instagram",
                            item["account"], item["url"], item["note"])
        n += int(ok)
    return n


# ---------------------------------------------------------------------------
# 自动化闭环：从播放数据回流中发掘并晋升爆款源账号
# ---------------------------------------------------------------------------

def promote_burst_accounts(db, s=None, threshold_play: int = 2500) -> list[dict]:
    """从已发布作品的真实播放量中，发掘并晋升优质源账号入白名单。

    数据闭环原理（2026-11-10 引入）：
    1. 标签探索抓取生面孔作者作品入库并发布。
    2. 播放数据回流（play_count 落库）。
    3. 若该视频播放量 >= threshold_play，由公域真实推荐验证为爆款源。
    4. 自动加入对应赛道的白名单（带题材闸门校验）。
    5. 下次采集时，优先从该账号的 /reels/ 扫出整套同系列作品。

    返回新晋升成功的账号信息列表。
    """
    lanes = ["kuaishou", "kuaishou2"]
    if s:
        lanes = [n for n, p in s.platforms.items() if getattr(p, "discover", None) and p.discover.get("enabled")]

    if not lanes:
        return []

    ph = ",".join("?" * len(lanes))
    with db._lock:
        rows = db.conn.execute(f"""
            SELECT j.platform as lane, t.username, t.play_count, t.title, 
                   t.source_url, t.source_platform, t.caption
            FROM publish_jobs j
            JOIN tasks t ON j.task_id = t.id
            WHERE j.state = 'PUBLISHED'
              AND j.platform IN ({ph})
              AND t.play_count IS NOT NULL
              AND t.play_count >= ?
              AND t.source_platform IN ('instagram', 'youtube')
            ORDER BY t.play_count DESC
        """, (*lanes, threshold_play)).fetchall()

    existing_by_lane: dict[str, set[str]] = {}
    for lane, plats in list_accounts().items():
        existing_by_lane[lane] = set()
        for plat, items in plats.items():
            for it in items:
                existing_by_lane[lane].add(str(it.get("account") or "").strip().lower())

    promoted: list[dict] = []

    for r in rows:
        lane = r["lane"]
        src_plat = r["source_platform"]
        raw_user = (r["username"] or "").strip()
        play = r["play_count"]
        title = (r["title"] or "").strip()
        caption = r["caption"] or ""

        # 提取合法 handle
        handle = ""
        if re.match(r"^[a-zA-Z0-9._]{2,30}$", raw_user):
            handle = raw_user
        elif src_plat == "instagram":
            cleaned = re.sub(r"[^a-zA-Z0-9._]", "", raw_user)
            if 3 <= len(cleaned) <= 30:
                handle = cleaned

        if not handle or handle.lower() in _IG_RESERVED:
            continue

        handle_lower = handle.lower()
        lane_existing = existing_by_lane.setdefault(lane, set())
        if handle_lower in lane_existing:
            continue

        # 题材闸门二次校验：防止非本赛道的偶发内容被错误晋级
        text = f"{title} {caption}".lower()
        if s and lane in s.platforms:
            try:
                from .discover import match_keywords
                conf = s.platforms[lane].discover or {}
                req = conf.get("require_any") or []
                block = conf.get("block_any") or []
                if block and match_keywords(text, block):
                    continue
                if req and not match_keywords(text, req):
                    continue
            except Exception:
                pass

        if src_plat == "instagram":
            profile_url = f"https://www.instagram.com/{handle}/"
        else:
            profile_url = f"https://www.youtube.com/@{handle}"

        note = f"实测爆款晋升: 播{play} 《{title[:20]}》"
        ok, msg = add_account(lane, src_plat, handle, profile_url, note)
        if ok:
            lane_existing.add(handle_lower)
            promoted.append({
                "lane": lane,
                "platform": src_plat,
                "account": handle,
                "url": profile_url,
                "play_count": play,
                "title": title[:30],
            })
            log.info("🌟 爆款源账号自动晋级白名单 [%s/%s]: @%s (播 %d)",
                     lane, src_plat, handle, play)

    return promoted

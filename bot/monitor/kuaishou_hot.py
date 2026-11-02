"""快手热门话题抓取与存储。

每天早上 8:00 由阿里云机（国内原生网络）从快手主站抓取实时热榜，
并通过 POST /api/hot_topics 发送给日本协调端保存到 data/kuaishou_hot_topics.json。
协调端在文案生成时载入该热门话题表，供大模型优先匹配并输出最具热度的话题。
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime

from ..config import DATA_DIR

log = logging.getLogger(__name__)

KS_HOT_FILE = DATA_DIR / "kuaishou_hot_topics.json"


def fetch_kuaishou_hot_topics(timeout: float = 15.0) -> list[dict]:
    """从快手主站抓取实时热榜（Top 50）。

    返回：
    [
        {"rank": 1, "topic": "...", "hot_value": "1256.1万"},
        ...
    ]
    """
    url = "https://www.kuaishou.com/?isHome=1"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log.warning("快手热榜请求失败: %s", e)
        return []

    start = html.find("window.__APOLLO_STATE__=")
    if start == -1:
        log.warning("快手主页未找到 __APOLLO_STATE__")
        return []

    script = html[start + len("window.__APOLLO_STATE__=") :]
    end = script.find(";(function(")
    if end == -1:
        end = script.find("</script>")
    if end == -1:
        log.warning("快手主页 __APOLLO_STATE__ 结尾未匹配")
        return []

    raw = script[:end].strip().rstrip(";")
    r_brace = raw.rfind("}")
    if r_brace != -1:
        raw = raw[: r_brace + 1]

    try:
        data = json.loads(raw).get("defaultClient", {})
    except Exception as e:
        log.warning("快手 __APOLLO_STATE__ JSON 解析失败: %s", e)
        return []

    # 兼容多种 Apollo 存储格式：
    # 1. data 顶层以 $ROOT_QUERY.visionHotRank(...) 为 key
    # 2. data["ROOT_QUERY"]["visionHotRank(...)"]
    rank_obj = None
    for k, v in data.items():
        if "visionHotRank" in k and isinstance(v, dict):
            rank_obj = v
            break
    if not rank_obj and isinstance(data.get("ROOT_QUERY"), dict):
        for k, v in data["ROOT_QUERY"].items():
            if "visionHotRank" in k and isinstance(v, dict):
                rank_obj = v
                break

    if not rank_obj:
        log.warning("快手数据中未找到 visionHotRank")
        return []

    items = rank_obj.get("items", [])
    results: list[dict] = []
    rank = 1
    for it in items:
        item_id = it.get("id") if isinstance(it, dict) else str(it)
        if not item_id:
            continue
        obj = data.get(item_id)
        if not isinstance(obj, dict):
            continue
        name = str(obj.get("name") or "").strip()
        hot = str(obj.get("hotValue") or "").strip()
        tag_type = obj.get("tagType")
        if not name or tag_type == "置顶":
            continue
        results.append({
            "rank": rank,
            "topic": name,
            "hot_value": hot,
        })
        rank += 1
    return results


def load_cached_hot_topics(max_age_hours: float = 48.0) -> list[dict]:
    """读取本地缓存的快手热门话题。"""
    if not KS_HOT_FILE.exists():
        return []
    try:
        data = json.loads(KS_HOT_FILE.read_text(encoding="utf-8"))
        updated_at = data.get("updated_at")
        if updated_at:
            dt = datetime.fromisoformat(updated_at)
            age = (datetime.now() - dt).total_seconds() / 3600
            if age > max_age_hours:
                log.debug("快手热门话题缓存已过期 (%.1fh)", age)
        return data.get("topics") or []
    except Exception as e:
        log.debug("读取快手热门话题缓存失败: %s", e)
        return []


def format_hot_topics_for_prompt(topics: list[dict], limit: int = 30) -> str:
    """将热门话题格式化为供 LLM Prompt 使用的文本列表。"""
    if not topics:
        return ""
    lines = []
    for it in topics[:limit]:
        rank = it.get("rank")
        topic = it.get("topic")
        hot = it.get("hot_value")
        if topic:
            s = f"{rank}. {topic}" + (f" (热度 {hot})" if hot else "")
            lines.append(s)
    return "\n".join(lines)

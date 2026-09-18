"""播放量回流：从各平台创作者后台抓取作品数据，写回 tasks 表。

动机（2026-09-19）
    tasks 表原本**完全没有播放量字段**，AI 生成话题只能靠先验审美，
    结果话题退化成「治愈/解压/纯享」近义词排列组合（详见
    bot/ai/tag_quality.py）。没有数据回流，就没有优化闭环。
    本模块补上这条回路：抓取 → 落库（play_count/like_count/stats_at）
    → 供话题池统计使用。

平台覆盖
    抖音  —— 已验证可用（creator.douyin.com 内容管理页，卡片文本解析）
    快手  —— 依赖登录态；未登录时跳过并告警
    视频号 —— 暂未实现（后台结构不同）

解析策略
    不做脆弱的整页 DOM 选择器，而是：
      1. 找出所有「删除作品」按钮（每个作品卡恰好一个）
      2. 从按钮向上找最近的、同时含「播放」和「完播率」的祖先节点 = 卡片
      3. 对卡片文本做正则抽取（发布日期 / 播放 / 点赞）
    这套解析在抖音改版后依然有效，因为它依赖的是**语义文本**而非 class 名。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

DOUYIN_MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"
KUAISHOU_MANAGE_URL = "https://cp.kuaishou.com/article/manage"


@dataclass
class WorkStat:
    """一条作品的平台侧数据。"""
    published_at: str = ""
    play_count: int | None = None
    like_count: int | None = None
    tags: list[str] = field(default_factory=list)
    title_hint: str = ""


# ---------------------------------------------------------------------------
# 文本解析（纯函数，便于单测）
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}:\d{2})")
# 快手日期是短横线格式：2026-09-18 12:12（与抖音的中文格式不同）
_DATE_DASH_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}:\d{2})")
_PLAY_RE = re.compile(r"播放\s*(\d+)")
_LIKE_RE = re.compile(r"点赞\s*(\d+)")
# 快手卡片没有「播放」标签，三列裸数字（播放/评论/点赞）直接跟在日期后面，
# 且数字用中文缩写：267 / 3,590 / 1.7万 / 11.4万。
_NUM_LINE_RE = re.compile(r"^(\d+(?:,\d{3})*(?:\.\d+)?)(万)?$")
# 判定列序的依据（2026-09-19 实测）：67.4万 播放的作品三列是 98 / 2,784 ——
# 98 只能是评论、2784 是点赞；反过来（98赞2784评论）不合常理。
# 所以列序固定：播放 / 评论 / 点赞。


def _parse_cn_number(s: str) -> int:
    """'267' → 267；'3,590' → 3590；'1.7万' → 17000；'11.4万' → 114000。"""
    s = (s or "").strip().replace(",", "")
    m = re.match(r"^(\d+(?:\.\d+)?)(万)?$", s)
    if not m:
        return 0
    val = float(m.group(1))
    if m.group(2):
        val *= 10000
    return int(val)
# 话题：从「#」到**下一个 # 或开头**为止，取最左边一截。
# 卡片文本是连续拼接的（"…#解压编辑作品设置权限作品置顶删除作品2026年…"），
# 所以先按下一个 # / 到串首 切段，再把段内的 UI 文案剥掉。
# 注意不能把空格当分隔 —— #无声视频 #治愈 之间是空格，那是合法的多个标签。
_TAG_RE = re.compile(r"#(.+?)(?=\s*#|\Z)", re.S)

# 卡片里跟在最后一个话题后面的固定 UI 文案，需要剥掉
_TAIL_WORDS = ("编辑作品", "设置权限", "作品置顶", "删除作品", "取消置顶",
               "查看详情", "导出数据", "已发布", "审核中", "未通过",
               "仅自己可见", "流量减少", "播放", "点赞", "评论", "分享",
               "收藏", "完播率", "2秒跳出率", "吸粉量", "作品合集")


def _clean_tag(raw: str) -> str:
    """剥掉话题尾巴上粘连的 UI 文案与日期。"""
    t = raw.strip()
    if not t:
        return ""
    # 切掉任何 UI 关键词及其之后的内容
    for w in _TAIL_WORDS:
        if w in t:
            t = t.split(w)[0]
    t = _DATE_RE.split(t)[0]
    # 截断到第一个明显不是标签的字符（年月日数字串等）
    t = re.split(r"\d{4}年|\d{2}:\d{2}", t)[0]
    return t.strip()


def parse_card_text(text: str) -> WorkStat | None:
    """从单张作品卡片的文本里抽数据。抽不到播放量返回 None。"""
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t:
        return None

    play_m = _PLAY_RE.search(t)
    if not play_m:
        return None

    st = WorkStat()
    st.play_count = int(play_m.group(1))

    like_m = _LIKE_RE.search(t)
    if like_m:
        st.like_count = int(like_m.group(1))

    dt_m = _DATE_RE.search(t)
    if dt_m:
        y, mo, d, hm = dt_m.groups()
        st.published_at = f"{y}-{int(mo):02d}-{int(d):02d} {hm}"

    # 话题：剥掉粘连的 UI 文案尾巴
    tags: list[str] = []
    for raw in _TAG_RE.findall(t):
        tag = _clean_tag(raw)
        if not tag or len(tag) > 12 or tag in tags:
            continue
        tags.append(tag)
    st.tags = tags[:10]

    head = t.split("编辑作品")[0].strip()
    st.title_hint = head[:80]

    return st


def _dedupe(stats: list[WorkStat]) -> list[WorkStat]:
    """同一作品在卡片嵌套里可能出现两次，按 (日期, 播放, 话题) 去重。"""
    seen: set[tuple] = set()
    out: list[WorkStat] = []
    for s in stats:
        key = (s.published_at, s.play_count, tuple(s.tags))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

# 在页面里收集所有作品卡片的文本。
# 依赖语义（"删除作品" 按钮 + "播放"/"完播率" 字段），不依赖易变的 class 名，
# 所以对平台改版有一定韧性（2026-09-18 抖音改版验证过）。
_JS_COLLECT_CARDS = """() => {
    const out = [];
    const btns = [...document.querySelectorAll('*')]
        .filter(e => (e.textContent || '').trim() === '删除作品');
    for (const b of btns) {
        let card = b;
        for (let i = 0; i < 12 && card.parentElement; i++) {
            card = card.parentElement;
            const t = card.textContent || '';
            if (t.includes('播放') && (t.includes('完播率') || t.includes('点赞'))) break;
        }
        out.push((card.textContent || '').replace(/\\s+/g, ' ').trim());
    }
    return out;
}"""


def fetch_douyin_stats(page, settle_ms: int = 10_000) -> list[WorkStat]:
    """从抖音内容管理页抓全部作品数据（page 需已登录）。"""
    try:
        page.wait_for_timeout(settle_ms)
        cards = page.evaluate(_JS_COLLECT_CARDS)
    except Exception as e:
        log.warning("抖音作品数据抓取失败：%s", str(e)[:120])
        return []

    stats: list[WorkStat] = []
    for c in cards or []:
        st = parse_card_text(c)
        if st:
            stats.append(st)
    stats = _dedupe(stats)
    log.info("抖音作品数据：解析到 %d 条", len(stats))
    return stats


def fetch_kuaishou_stats(page, settle_ms: int = 12_000) -> list[WorkStat]:
    """从快手作品管理页抓数据（需登录态有效）。

    快手卡片结构与抖音完全不同（2026-09-19 实测）：

        00:49                        ← MM:SS 时长行 = 卡片天然分界
        【纯享放松】安安静静看完全程… #无声视频 #纯享放松 #治愈系 #视觉解压
        已发布
        2026-09-18 10:06             ← 短横线日期
         1.7万                        ← 播放（中文缩写，无「播放」字样）
         3                            ← 评论
         83                           ← 点赞

    三列裸数字固定顺序：播放 / 评论 / 点赞。列序判定依据：67.4万 播放的
    作品三列是 98 / 2,784 —— 98 只能是评论、2784 是点赞，反了不合常理。
    """
    try:
        page.wait_for_timeout(settle_ms)
        body = page.locator("body").inner_text(timeout=15_000)
    except Exception as e:
        log.warning("快手作品数据抓取失败：%s", str(e)[:120])
        return []

    # 未登录检测：跳回落地页会出现「立即登录」
    if "立即登录" in body and len(body) < 3000:
        log.warning("快手登录态已失效，跳过作品数据抓取")
        return []

    # 按 MM:SS / H:MM:SS 时长行切块（每块 = 一张作品卡片的完整文本）
    blocks = re.split(r"(?m)^(?=\d{1,2}:\d{2}(?::\d{2})?\s*$)", body)

    stats: list[WorkStat] = []
    for blk in blocks:
        st = _parse_ks_block(blk)
        if st:
            stats.append(st)
    stats = _dedupe(stats)
    log.info("快手作品数据：解析到 %d 条", len(stats))
    return stats


def _parse_ks_block(blk: str) -> WorkStat | None:
    """解析单张快手卡片文本块。"""
    t = re.sub(r"[ \t]+", " ", blk).strip()
    if not t:
        return None

    st = WorkStat()

    # 日期：短横线格式（快手的与抖音中文格式不同）
    dm = _DATE_DASH_RE.search(t)
    if dm:
        y, mo, d, hm = dm.groups()
        st.published_at = f"{y}-{int(mo):02d}-{int(d):02d} {hm}"
    else:
        # 兼容中文格式（万一页面结构变体）
        dm2 = _DATE_RE.search(t)
        if dm2:
            y, mo, d, hm = dm2.groups()
            st.published_at = f"{y}-{int(mo):02d}-{int(d):02d} {hm}"

    # 话题：# 开头到下一个 # / 行尾
    st.tags = []
    for raw in re.findall(r"#([^\s#]+)", t):
        tag = raw.strip()
        if tag and len(tag) <= 12 and tag not in st.tags:
            st.tags.append(tag)
    st.tags = st.tags[:10]

    # 三列数字：日期行之后的裸数字行（可带万缩写），固定 播放/评论/点赞
    date_end = dm.end() if dm else 0
    nums: list[int] = []
    for line in t[date_end:].splitlines():
        line = line.strip()
        if not line:
            continue
        m = _NUM_LINE_RE.match(line)
        if m:
            nums.append(_parse_cn_number(line))
        elif nums:
            break   # 数字列结束了
    if len(nums) >= 3:
        st.play_count = nums[0]
        st.like_count = nums[2]
    elif len(nums) == 1:
        # 只有播放列（评论/点赞为 0 时页面可能不渲染）
        st.play_count = nums[0]

    if not st.play_count or not st.published_at:
        return None

    # 标题提示：时长行之后、日期之前的第一段长文本
    head = t.split("已发布")[0]
    st.title_hint = head[:80]

    return st


# ---------------------------------------------------------------------------
# 统计：给话题池用
# ---------------------------------------------------------------------------

def top_performing_tags(stats: list[WorkStat], min_play: int = 500,
                        limit: int = 20) -> list[tuple[str, int, int]]:
    """统计高播放作品用过的话题。

    返回 [(tag, 出现次数, 该 tag 下的最高播放)]，按最高播放降序。
    """
    agg: dict[str, list[int]] = {}
    for s in stats:
        if not s.play_count or s.play_count < min_play:
            continue
        for t in s.tags:
            agg.setdefault(t, []).append(s.play_count)

    rows = [(tag, len(plays), max(plays)) for tag, plays in agg.items()]
    rows.sort(key=lambda x: (-x[2], -x[1]))
    return rows[:limit]


def summarize(stats: list[WorkStat]) -> dict:
    """给日志/Telegram 用的摘要。"""
    plays = [s.play_count for s in stats if s.play_count is not None]
    if not plays:
        return {"count": 0}
    plays.sort(reverse=True)
    return {
        "count": len(plays),
        "max": plays[0],
        "min": plays[-1],
        "avg": round(sum(plays) / len(plays)),
        "median": plays[len(plays) // 2],
    }


# ---------------------------------------------------------------------------
# 回写：把平台数据匹配到本地 task
# ---------------------------------------------------------------------------

def _parse_dt(s: str):
    """把时间字符串解析成 datetime；失败返回 None。

    必须支持 ISO 的 'T' 分隔符 —— tasks.published_at 存的就是
    '2026-09-18T10:03:00' 这种格式，解析不了会导致匹配恒失败、
    播放量一条都回写不进去（实测踩过）。
    """
    from datetime import datetime
    s = (s or "").strip()
    if not s:
        return None
    s = s.replace("T", " ")
    if "." in s:                       # 去掉微秒
        s = s.split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y年%m月%d日 %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def match_task(st: WorkStat, tasks: list[dict], tolerance_min: int = 90) -> dict | None:
    """把一条平台数据匹配到本地已发布作品。

    匹配优先级：
      1. 发布时间最近且差在 tolerance_min 分钟内
      2. 话题集合有交集（辅助确认）
    只按「时间接近」可能错配（同平台连发），加话题交集降低误配率。
    """
    from datetime import timedelta

    st_dt = _parse_dt(st.published_at)
    if not st_dt:
        return None

    cands: list[tuple[int, dict]] = []
    for t in tasks:
        # 注意：不要手工截断/替换 —— _parse_dt 已处理 ISO 'T' 与秒。
        t_dt = _parse_dt(t.get("published_at") or "")
        if not t_dt:
            continue
        gap = abs((t_dt - st_dt).total_seconds()) / 60.0
        if gap > tolerance_min:
            continue
        # 话题交集加分
        local_tags = []
        raw = t.get("tags") or ""
        try:
            import json as _json
            j = _json.loads(raw)
            local_tags = j if isinstance(j, list) else []
        except Exception:
            local_tags = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        overlap = len(set(map(str, local_tags)) & set(st.tags))
        score = overlap * 100 - gap   # 交集优先，其次时间最近
        cands.append((int(score), t))

    if not cands:
        return None
    cands.sort(key=lambda x: -x[0])
    return cands[0][1]


def persist(db, stats: list[WorkStat], platform: str = "douyin") -> dict:
    """把抓到的平台数据写回 tasks 表。

    返回 {"matched": n, "updated": n, "unmatched": n}
    """
    from datetime import datetime

    # 取该平台已发布的作品
    with db._lock:
        rows = db.conn.execute(
            """SELECT * FROM tasks
               WHERE published_at IS NOT NULL AND state IN ('NOTIFIED','READY')
               ORDER BY published_at DESC LIMIT 300"""
        ).fetchall()
    tasks = [dict(r) for r in rows]

    matched = updated = 0
    for st in stats:
        t = match_task(st, tasks)
        if not t:
            continue
        matched += 1
        try:
            db.update(
                t["id"],
                play_count=st.play_count,
                like_count=st.like_count,
                stats_at=datetime.now().isoformat(timespec="seconds"),
            )
            updated += 1
            log.debug("回写播放量：task=%s play=%s", t["id"], st.play_count)
        except Exception as e:
            log.warning("回写播放量失败 task=%s：%s", t["id"], str(e)[:100])

    result = {"matched": matched, "updated": updated,
              "unmatched": len(stats) - matched}
    log.info("播放量回流（%s）：抓到 %d 条，匹配 %d，回写 %d，未匹配 %d",
             platform, len(stats), matched, updated, result["unmatched"])
    return result


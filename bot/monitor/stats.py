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
from datetime import datetime

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


def _work_stat_from_photo_item(v: dict) -> WorkStat | None:
    """从快手官方 photo/list 响应对象构建 WorkStat（零正则解析，数据绝对精确）。"""
    if not isinstance(v, dict):
        return None
    st = WorkStat()
    st.play_count = int(v.get("playCount") or 0)
    st.like_count = int(v.get("likeCount") or 0)

    upload_time = v.get("uploadTime")
    if upload_time:
        try:
            ts = float(upload_time)
            if ts > 1e11:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts)
            st.published_at = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

    title = (v.get("title") or "").strip()
    st.title_hint = title[:80]

    tags: list[str] = []
    for raw in re.findall(r"#([^\s#]+)", title):
        tag = raw.strip()
        if tag and len(tag) <= 12 and tag not in tags:
            tags.append(tag)
    st.tags = tags[:10]

    if not st.published_at and st.play_count == 0:
        return None
    return st


def fetch_kuaishou_stats(page, settle_ms: int = 12_000) -> list[WorkStat]:
    """从快手作品管理页抓数据（需登录态有效）。

    优先通过网络嗅探截获页面自身发出的 photo/list 结构化 JSON（借鉴 MatrixMedia），
    数据零误差且无需反编译签名；如果未嗅探到数据，自动回退到页面 DOM 文本正则解析。
    """
    captured_items: list[dict] = []

    def _on_response(response):
        try:
            url = getattr(response, "url", "")
            if "photo/list" in url and getattr(response, "status", 0) == 200:
                data = response.json()
                if isinstance(data, dict) and data.get("result") == 1:
                    lst = (data.get("data") or {}).get("list") or []
                    for it in lst:
                        if isinstance(it, dict):
                            captured_items.append(it)
        except Exception:
            pass

    try:
        page.on("response", _on_response)
    except Exception:
        pass

    try:
        # 页面滚动，触发懒加载与 photo/list 网络请求
        page.wait_for_timeout(min(settle_ms, 3000))
        page.evaluate("""() => {
            window.scrollTo(0, document.documentElement.scrollHeight);
            document.querySelectorAll('*').forEach(el => {
                if (el.scrollHeight > el.clientHeight + 100 && el.clientHeight > 200) {
                    el.scrollTop = el.scrollHeight;
                }
            });
        }""")
        page.wait_for_timeout(min(settle_ms, 3000))
    except Exception as e:
        log.debug("快手页面滚动触发异常：%s", e)

    # 1. 优先使用网络嗅探捕获的精确数据
    if captured_items:
        sniffed_stats: list[WorkStat] = []
        for it in captured_items:
            st = _work_stat_from_photo_item(it)
            if st:
                sniffed_stats.append(st)
        if sniffed_stats:
            sniffed_stats = _dedupe(sniffed_stats)
            log.info("快手作品数据：通过网络嗅探(photo/list)捕获到 %d 条精确数据", len(sniffed_stats))
            return sniffed_stats

    # 2. 兜底回退：原有 DOM 文本正则解析
    try:
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
    log.info("快手作品数据：DOM文本解析到 %d 条", len(stats))
    return stats


def fetch_kuaishou_user_info(page) -> dict | None:
    """读取快手创作者粉丝总量等概览数据（借鉴 MatrixMedia，无需复杂签名）。"""
    try:
        res = page.evaluate("""async () => {
            const m = document.cookie.match(/(?:^|;\\s*)kuaishou\\.web\\.cp\\.api_ph=([^;]+)/);
            const apiPh = m ? m[1] : '';
            const resp = await fetch('/rest/cp/creator/pc/home/userInfo', {
                method: 'POST',
                headers: {
                    'content-type': 'application/json;charset=UTF-8',
                    'accept': 'application/json, text/plain, */*'
                },
                credentials: 'include',
                body: JSON.stringify({ "kuaishou.web.cp.api_ph": apiPh })
            });
            return await resp.json();
        }""")
        if isinstance(res, dict) and res.get("result") == 1:
            core = (res.get("data") or {}).get("coreUserInfo") or {}
            return {
                "fans": int(core.get("fansNum") or 0),
                "user_id": core.get("userId"),
                "name": core.get("userName"),
            }
    except Exception as e:
        log.debug("获取快手创作者信息失败：%s", e)
    return None


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
    """把抓到的平台数据写回 tasks 表，并留存历史快照。

    返回 {"matched": n, "updated": n, "unmatched": n, "snapshots": n}

    为什么要留快照（2026-11-09）
    ---------------------------
    tasks.play_count 只保留「最后一次测量值」，而各条作品的最后测量时间
    并不相同。实测数据里，10-30 发布的测于 237h 后（中位 382），
    11-09 发布的测于 6h 后（中位 20）—— 直接比会得出「暴跌 19 倍」，
    其中很大一部分只是长尾曲线上的位置差。

    所以每次测量都落一条 play_snapshots，之后才能按「发布满 N 小时」
    对齐比较，或看同一条作品的增长曲线。
    """
    from datetime import datetime

    # 取该平台已发布的作品。必须按 platform 过滤（join publish_jobs）：
    # 多号矩阵下两号发布时间接近，不过滤会把 A 号页面数据写到 B 号任务上。
    with db._lock:
        rows = db.conn.execute(
            """SELECT t.* FROM tasks t
               WHERE t.published_at IS NOT NULL AND t.state IN ('NOTIFIED','READY')
                 AND EXISTS (SELECT 1 FROM publish_jobs j
                             WHERE j.task_id = t.id AND j.platform = ?
                               AND j.state = 'PUBLISHED')
               ORDER BY t.published_at DESC LIMIT 300""",
            (platform,),
        ).fetchall()
    tasks = [dict(r) for r in rows]

    matched = updated = snapshots = 0
    measured_at = datetime.now().isoformat(timespec="seconds")
    for st in stats:
        t = match_task(st, tasks)
        if not t:
            continue
        matched += 1
        # 测量时距发布已过多少小时 —— 后面按年龄对齐全靠它
        age_hours = None
        try:
            pub = _parse_dt(t.get("published_at") or "")
            if pub:
                age_hours = (datetime.now() - pub).total_seconds() / 3600
        except Exception:
            age_hours = None
        try:
            db.update(
                t["id"],
                play_count=st.play_count,
                like_count=st.like_count,
                stats_at=measured_at,
            )
            updated += 1
            log.debug("回写播放量：task=%s play=%s age=%.0fh",
                      t["id"], st.play_count, age_hours or 0)
        except Exception as e:
            log.warning("回写播放量失败 task=%s：%s", t["id"], str(e)[:100])
        # 快照独立于 tasks 更新：即使 tasks 更新失败也值得留档
        try:
            if db.add_play_snapshot(t["id"], platform, st.play_count,
                                    st.like_count, measured_at, age_hours):
                snapshots += 1
        except Exception as e:
            log.debug("播放快照写入跳过 task=%s：%s", t["id"], str(e)[:80])

    result = {"matched": matched, "updated": updated,
              "unmatched": len(stats) - matched, "snapshots": snapshots}
    log.info("播放量回流（%s）：抓到 %d 条，匹配 %d，回写 %d，未匹配 %d，新快照 %d",
             platform, len(stats), matched, updated, result["unmatched"], snapshots)
    return result


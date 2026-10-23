"""LLM 文案生成：按平台 profile 生成各平台专属的标题/简介/标签（JSON）。

标题风格（2026-10-18 起）：9 月快手爆款那种短句笑点，不挂专栏前缀。
实测【有点视频】上线后单条播放从百万掉到几百，而 372 万那条是
「没声音却笑到停不下来 这画面太绝了」。
严禁凭空编造无中生有的剧情（如做饭带娃写作业）；必须基于画面实际内容拟写。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

from ..config import Settings
from . import tag_quality

log = logging.getLogger(__name__)

# 2026-09-19 曾强制【有点视频】前缀。2026-10 播放从百万掉到几百后弃用：
# 9 月爆款是裸短句（「没声音却笑到停不下来」），不是专栏标签。
# 模型若仍写出这些前缀，校验层剥掉，不再补回去。
LEGACY_PREFIXES = (
    "【有点视频】", "【无声治愈】", "【无声小剧场】", "【纯享放松】",
)

# 口吻套话：这些是**说话方式**上的烂梗，继续封。
# 注意：情绪化标签（#笑到肚子疼 / #无声也精彩 类）已按实测数据放开
# （2026-09-19），不要再把它们加回本表。详见 bot/ai/tag_quality.py。
BANNED_PATTERNS = [
    "心里踏实", "真踏实", "心里就踏实", "心里一下子",
    "喘口气", "歇口气",
    "姐妹们", "家人们", "兄弟们",
    "带娃做饭", "做饭带娃", "写作业", "做家务", "做饭",
]


@dataclass(frozen=True)
class PlatformProfile:
    name: str
    display_name: str
    max_title_len: int
    max_tags: int = 5
    supports_category: bool = False
    tone: str = ""
    desc_hint: str = ""
    # 标题风格段（空 = 默认搞笑钩子规范）。快手2号用自适应三口吻。
    title_style: str = ""
    # 话题组合：锚话题（空 = 不锚）、AI 内容话题最多占几席、固定池兜底词
    tag_base: str = "无声视频"
    tag_content_slots: int = 2
    tag_fixed_pool: tuple[str, ...] = ()


PLATFORM_PROFILES: dict[str, PlatformProfile] = {
    "kuaishou": PlatformProfile(
        name="kuaishou", display_name="快手", max_title_len=40, max_tags=4,
        supports_category=True,
        tone="风趣、诙谐、像朋友憋着笑转述，短、有钩子，不解释画面",
        desc_hint="1~2 句话点出画面里真正发生的事，不要把标题再抄一遍",
    ),
    # 快手2号：女性生活内容（四件套/睡衣展示、化妆/盘发教程）。
    # 标题口吻按内容类型自适应；话题不用「无声视频」锚——教程类有人声，
    # 挂无声标签会伤账号垂直度，锚位让给 AI 按内容选。
    "kuaishou2": PlatformProfile(
        name="kuaishou2", display_name="快手2号", max_title_len=40, max_tags=4,
        supports_category=True,
        tone="面向女性的种草/教程口吻：好物展示重质感场景，教程重简单易学",
        desc_hint="1~2 句话点出卖点或教程要点（面料/版型/步骤/适合场景），不要抄标题",
        title_style=(
            "2. 先判断这条内容属于哪类，就用哪类口吻写标题（自适应）：\n"
            "   - 家纺四件套 / 睡衣展示 → 质感种草：面料手感、配色、卧室焕新的\n"
            "     场景代入，让人想象自己用上的样子；\n"
            "   - 化妆技巧 → 干货教程：点出最关键的一步或前后对比，\n"
            "     「手残党也能学会」式低门槛钩子；\n"
            "   - 盘发/穿搭技巧 → 场景速成：通勤/约会/日常几步搞定，\n"
            "     强调省时省手、学会就赚到。\n"
            "   只学口气，禁止照抄；卖点必须来自画面和转录里的真实信息。"
        ),
        tag_base="",
        tag_content_slots=3,
        tag_fixed_pool=("好物分享", "变美日常", "穿搭分享", "美妆教程", "生活好物"),
    ),
    "douyin": PlatformProfile(
        name="douyin", display_name="抖音", max_title_len=55, max_tags=5,
        tone="画面感强、节奏轻快，第一句直接抓住视频核心看点",
        desc_hint="1~2 句话简洁描述趣味/温情瞬间，自然引出话题",
    ),
    "xhs": PlatformProfile(
        name="xhs", display_name="小红书", max_title_len=20, max_tags=4,
        tone="温柔真诚、分享欲满满，字数严格限制20字以内",
        desc_hint="笔记式口吻，突出视觉治愈感",
    ),
    "weixin": PlatformProfile(
        name="weixin", display_name="微信视频号", max_title_len=16, max_tags=4,
        tone="温和内敛、正向治愈，包含前缀必须严格在6~16字以内，短标题严禁使用【】方括号，可用书名号《》或纯文本",
        desc_hint="简洁有质感，突出真挚情感或静心体验",
    ),
    "toutiao": PlatformProfile(
        name="toutiao", display_name="今日头条", max_title_len=30, max_tags=4,
        tone="口语短句、有具体看点。标题硬限 30 字，不带专栏前缀",
        desc_hint="1~2 句话点出画面里真正发生的事，不要把标题再抄一遍",
    ),
}


class CopywriterError(RuntimeError):
    pass


def _system_prompt(profile: PlatformProfile) -> str:
    category_rule = (
        '"category": "必须从给定的可选分区列表中选择最匹配的一个（如萌宠/搞笑/生活/情感/二次元）"'
        if profile.supports_category else
        '"category": ""（该平台无分区，固定输出空字符串）'
    )
    category_req = (
        "- category 必须严格等于分区列表中的某一项"
        if profile.supports_category else
        "- category 固定输出空字符串"
    )

    banned_str = "、".join(f"「{p}」" for p in BANNED_PATTERNS[:10])
    style_block = profile.title_style or (
        "2. 先写笑点，不写旁白。一句里要有「谁 + 反差/误会/吃瘪」，把最炸的结果咽住半句当钩子。\n"
        "   只学口气，禁止照抄，必须换成这条画面里真实发生的事：\n"
        "   - 难怪充电这么慢，墙里那只仓鼠刚被踹醒\n"
        "   - 猎豹啃了半天，对方缩成球让它干生气\n"
        "   - 吉娃娃把肇事狗直接拎到家长面前\n"
        "   - 奶奶才带两天，孩子已经被喂成球了"
    )

    if profile.name == "kuaishou2":
        role = ("你是快手女性生活类账号的标题写手。素材是面向女性的好物展示与教程："
                "家纺四件套、睡衣展示、化妆技巧、盘发/穿搭技巧等。")
        style_lead = "标题要像刷到就想点开的那种种草句/干货句，短、有钩子，不是广告喊话。"
    else:
        role = ("你是快手搞笑动画账号的标题写手。素材多为 AI 3D 动画、"
                "萌宠小动物剧情、无对白的幽默短片。")
        style_lead = "标题要像 2026 年 9 月打出过百万播放的那种短句，而不是专栏海报。"

    return f"""{role}
{style_lead}

【输入信息】
你会拿到三份材料，**可信度从高到低**：
1. 【画面实际内容】—— 由视觉模型逐帧看片后的客观描述（最高可信，以此为准）
2. 【视频来源文案】—— 发布者写的外文简介（含真实剧情线索，但常混引流话术，需甄别）
3. 【语音转录文本】—— 多为空（本专栏素材 92% 是无声视频）

你的任务：
1. 从【画面实际内容】里认出主角（具体物种/外貌特征）和它真正做了什么；
   来源文案若与画面冲突，一律以画面为准。
2. 来源文案里只提取**剧情线索**，忽略 Follow/Link in bio/话题标签等引流话术。
3. 严禁无中生有！严禁捏造与画面无关的人类琐事（绝对禁止编造：做饭、带娃、写作业、婆媳、家庭主妇等无关剧情）。

【标题规范（极其重要）】
1. **不要专栏前缀**，也不要【】。标题就是一句能让人想点开的话。
{style_block}
3. 要求：
   - **短**：12~24 个字，最多两小句。像一句吐槽，不是剧情简介；
   - **诙谐**：用拟人、反差、误会制造笑点，不要平铺「谁做了什么然后怎样」；
   - **有钩子**：看完还想知道后面，不要把结局和评价都写完；
   - **别用情绪垫句**。下面这些已经每条都在用，出现即失败：
       没声音、笑到停不下来、太绝了、更上头、笑出腹肌、这画面、看得停不下来、
       笑出声、停不下来
     另外这些词也被用烂了，禁止：
       走位、翻车、滑跪、脸刹、天衣无缝、王者、帅不过、下一秒、自以为、
       原地、打滑、起飞、六亲不认、残影、有点视频
     不要用「它觉得/它以为/本以为」开头。

标题总字数严格不得超过 {profile.max_title_len} 字！

【严禁句式】
严禁出现这些已被用烂的套话：{banned_str}，禁止以「姐妹们你们呢」等俗套口吻结尾。

【话题（tags）—— 按内容给，不照抄固定组合】
给 2~3 个**贴合这条内容**的话题词，每个不超过 8 个字，
内容属于什么领域就写什么（动画萌宠如「萌宠日常」；
女性生活如「好物分享」「美妆教程」「居家好物」）。
每条不一样；发布端会自动带上账号基础话题并按平台补齐数量，
你只负责内容相关的那几个。

【输出格式】
只输出一个合法的 JSON 对象，不要输出任何代码块标记、思考过程或多余说明：
{{
  "title": "一句贴合账号口吻的短钩子，不带【】、不带「没声音/太绝了」垫句（不超过 {profile.max_title_len} 字）",
  "description": "{profile.desc_hint}（1~2句话，真实呼应视频内容）",
  "tags": ["贴合画面内容的话题词1", "话题词2"],
  {category_rule}
}}

{category_req}
- 不得出现 Instagram、ins、YouTube、搬运、原作者、水印等字眼。"""


def _client(s: Settings):
    import httpx
    from openai import OpenAI
    if not s.ai_api_key:
        raise CopywriterError("缺少 AI_API_KEY：请在 .env 填写后重试")
    return OpenAI(base_url=s.ai_base_url, api_key=s.ai_api_key,
                  timeout=httpx.Timeout(180, connect=15))


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError(f"输出中找不到 JSON：{text[:200]}")
        return json.loads(m.group(0))


def _validate(obj: dict, categories: list[str], profile: PlatformProfile) -> dict:
    if not isinstance(obj, dict):
        raise ValueError("输出不是 JSON 对象")
    title = str(obj.get("title", "")).strip().strip('"""')
    if not title:
        raise ValueError("title 为空")

    title = _strip_legacy_prefix(title)
    if not title:
        raise ValueError("title 为空")
    title = title[:profile.max_title_len]

    description = str(obj.get("description", "")).strip()[:400]
    # 简介不要再抄一遍标题（抖音标题框 + 简介框会叠成重复句子）
    if description.startswith(title):
        description = description[len(title):].lstrip(" \n，。；;:：")
    hook = title.strip()
    if hook and len(hook) >= 6 and description.startswith(hook):
        description = description[len(hook):].lstrip(" \n，。；;:：")
    tags_raw = obj.get("tags") or []
    if not isinstance(tags_raw, list):
        tags_raw = []
    tags = [str(t).lstrip("#").strip()[:10] for t in tags_raw if str(t).strip()]

    # 基础 IP 标签保障（tag_base 为空 = 该平台不锚基础话题，
    # 如 kuaishou2 女性生活号：教程类有人声，不挂「无声视频」）
    if profile.tag_base and profile.tag_base not in tags:
        tags.insert(0, profile.tag_base)
    tags = [t for t in tags if t]

    # 话题策略（2026-10-23 改）：锚 1 个 + AI 内容话题 + 固定池补齐。
    # 此前固定组合（无声视频+搞笑日常+沙雕视频+搞笑动画）每条完全一样：
    # 话题没有方差 → 回流的播放数据永远无法归因到话题上。
    # 现在让 AI 按画面选内容话题（家族去重防近义词堆叠），
    # 固定池只在数量不足时兜底，账号基础曝光池保持稳定。
    tags = merge_tags(tags, profile.max_tags, title=title,
                      base_tag=profile.tag_base,
                      content_slots=profile.tag_content_slots,
                      fixed_pool=profile.tag_fixed_pool or None)

    category = str(obj.get("category", "")).strip()
    if profile.supports_category and categories:
        if category not in categories:
            hits = [c for c in categories if category in c or c in category]
            category = hits[0] if hits else categories[0]
    else:
        category = ""

    return {"title": title, "description": description, "tags": tags, "category": category}


def merge_tags(model_tags: list[str], max_tags: int, title: str = "",
               base_tag: str = "无声视频",
               content_slots: int = 2,
               fixed_pool: tuple[str, ...] | list[str] | None = None) -> list[str]:
    """话题组合：锚 1 个 + AI 内容话题 + 固定池补齐。

    base_tag 非空时始终在第 1 位（账号辨识度）；为空则不锚，
    全部位置给 AI 内容话题 + 池（kuaishou2 女性生活号用）。
    AI 给的内容话题按家族去重后最多占 content_slots 席——既保留
    验证过的基础池，又让每条视频有内容相关的话题方差，
    播放回流后才能比较出哪个话题真的带流量。
    """
    from .tag_quality import tag_family
    pool = list(fixed_pool) if fixed_pool else [
        "搞笑日常", "沙雕视频", "搞笑动画", "笑到肚子疼",
    ]
    base = (base_tag or "").strip()

    picked: list[str] = []
    seen_families: set[str] = set()
    for t in model_tags or []:
        n = re.sub(r"\s+", "", (t or "").lstrip("#").strip())
        if not n or n == base or n in picked:
            continue
        fam = tag_family(n)
        if fam:
            if fam in seen_families:
                continue
            seen_families.add(fam)
        picked.append(n)
        if len(picked) >= max(1, content_slots):
            break

    out = ([base] if base else []) + picked
    for f in pool:
        if len(out) >= max_tags:
            break
        if f not in out:
            out.append(f)
    return out[:max_tags]


def _check_hallucination(title: str, desc: str) -> list[str]:
    """严格拦截无中生有的主妇/带娃/做饭幻觉。"""
    hits = []
    for pat in BANNED_PATTERNS:
        if pat in title:
            hits.append(f"标题含「{pat}」")
        elif pat in desc and pat in ("做饭", "写作业", "带娃", "姐妹们", "喘口气"):
            hits.append(f"简介含「{pat}」")
    return hits


# 标题套话：实测基线（2026-09-27，40 条已发标题）
#   下一秒 47%、直接 35%、天衣无缝 25%、自以为 22%、走位 20%
# 成因是这些词就写在 system prompt 的示例里，模型零输入时直接照抄。
# 这里硬拦截并重试，光在 prompt 里劝是不够的。
CLICHE_WORDS = [
    "走位", "翻车", "滑跪", "脸刹", "天衣无缝", "王者",
    "帅不过", "下一秒", "自以为", "原地", "打滑", "起飞",
    "六亲不认", "残影", "有点视频",
    # 2026-10-20：提示词里的例句被模型当成每条都要接的尾巴
    "没声音", "笑到停不下来", "太绝了", "更上头", "笑出腹肌",
    "这画面", "看得停不下来", "停不下来",
]
CLICHE_PREFIXES = ["它觉得", "它以为", "本以为"]


def _strip_legacy_prefix(title: str) -> str:
    """去掉已停用的专栏前缀，标题保持 9 月那种裸短句。"""
    t = (title or "").strip()
    for p in LEGACY_PREFIXES:
        if t.startswith(p):
            t = t[len(p):].strip()
            break
    else:
        for bare in ("有点视频", "无声治愈", "无声小剧场", "纯享放松"):
            if t.startswith(bare):
                t = t[len(bare):].lstrip("】 ：:，,")
                break
    return t.strip()


def _check_cliche(title: str) -> list[str]:
    """拦截被用烂的标题套话，返回命中的词表（空表=通过）。"""
    hits = [w for w in CLICHE_WORDS if w in title]
    if "【" in title[:8]:
        hits.append("专栏前缀")
    hook = _strip_legacy_prefix(title)
    for p in CLICHE_PREFIXES:
        if hook.startswith(p):
            hits.append(f"开头「{p}」")
    return hits


def generate_copy(transcript: str, caption: str, categories: list[str], s: Settings,
                  platform: str = "kuaishou", content: dict | None = None) -> dict:
    """生成具备统一 IP 标识并与视频画面高度相关的文案。

    content: 画面内容探针结果（bot.ai.content_probe.describe_video 的返回）。
             有它时文案基于真实画面生成；没有则退化为「源文案 + 转录」。
    """
    profile = PLATFORM_PROFILES.get(platform) or PLATFORM_PROFILES["kuaishou"]
    from .asr import _NOISE_RE
    raw = (transcript or "").strip()
    if raw and _NOISE_RE.search(raw) and len(raw) < 40:
        raw = ""

    # 画面描述放在最前：它是唯一能反映视频真实内容的输入。
    # 实测（2026-09-27）：caption 常是引流话术、transcript 92% 为空，
    # 只靠这两样时模型会照抄 system prompt 里的示例词。
    content_block = ""
    if content and content.get("summary"):
        content_block = (
            f"【画面实际内容（视觉模型逐帧看片所得，以此为准）】\n"
            f"主角：{content.get('subject') or '未知'}\n"
            f"动作：{content.get('action') or '未知'}\n"
            f"场景：{content.get('setting') or '未知'}\n"
            f"情节：{content.get('beats') or '未知'}\n"
            f"看点：{content.get('summary')}\n\n"
        )

    user_content = (
        content_block
        + f"【视频来源文案（仅取剧情线索，忽略引流话术与话题标签）】\n"
        f"{caption.strip() or '（无外文简介）'}\n\n"
        f"【语音转录文本】\n{raw or '（视频无对白/无人声，纯画面叙事）'}\n\n"
    )
    # 动态话题榜：把回流播放量算出的 Top-N 注入 prompt（每次生成现查现注入）。
    # 榜单为空（新库/没抓过数据）时返回空串，自然跳过 —— 数据回流是增强不是依赖。
    try:
        from ..db import Database
        from .hot_tags import top_tags, format_for_prompt
        ranked = top_tags(Database(), min_play=500, limit=10,
                          exclude={"无声视频"})
        hot_block = format_for_prompt(ranked)
        if hot_block:
            user_content += (
                f"\n\n【近期实测高播放话题榜（来自本账号真实数据，动态更新）】\n"
                f"{hot_block}\n"
                f"以上话题已被验证能进对应流量池。你可以直接选用其中与视频内容"
                f"相符的（相符才用，硬凑会伤账号），也可以用新的长尾词，"
                f"但必须遵守上面的多样性硬要求。\n"
            )
    except Exception as e:
        # 榜单失败绝不阻塞生成 —— 打日志后按无榜单处理
        log.debug("话题榜注入跳过：%s", str(e)[:100])

    if profile.supports_category and categories:
        user_content += f"【{profile.display_name}可选分区列表】\n{'、'.join(categories)}"
    else:
        user_content += f"【发布平台】\n{profile.display_name}（无分区，category 输出空字符串）"

    client = _client(s)
    sys_prompt = _system_prompt(profile)
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_content},
    ]

    last_err = ""
    last_result: dict | None = None
    for attempt in range(1, 4):
        try:
            resp = client.chat.completions.create(
                model=s.ai_model, messages=messages, temperature=0.7,
            )
            # 上游（中转/内容审核）可能返回空 choices，直接取 [0] 会抛
            # "list index out of range" —— 完全看不出是内容被拦还是接口抽风。
            if not getattr(resp, "choices", None):
                last_err = "接口返回空 choices（多为内容审核拦截或上游限流）"
                log.warning("[%s] 第 %d 次：%s", profile.name, attempt, last_err)
                time.sleep(2)
                continue
            content = resp.choices[0].message.content or ""
            result = _validate(_extract_json(content), categories, profile)
            last_result = result

            banned = _check_hallucination(result["title"], result["description"])
            if banned:
                last_err = "；".join(banned)
                log.warning("[%s] 文案命中禁用词（第 %d 次）：%s", profile.name, attempt, last_err)
                messages.append({"role": "user", "content": f"上次生成违规：{last_err}。请绝对不要出现任何做饭、带娃或俗套套话，只针对原文案角色与事件重新输出。"})
                continue

            cliche = _check_cliche(result["title"])
            if cliche:
                last_err = "、".join(cliche)
                log.warning("[%s] 标题命中套话（第 %d 次）：%s | %r",
                            profile.name, attempt, last_err, result["title"])
                messages.append({"role": "user", "content": (
                    f"上次标题用了这些被用烂的词：{last_err}。"
                    f"重写一句：诙谐、好懂、12 到 24 字，用反差或误会当钩子，"
                    f"不要平铺剧情，也不要加「没声音 / 太绝了 / 笑到停不下来」这种垫句。"
                    f"上次标题：{result['title']}"
                )})
                continue

            log.info("[%s] IP文案生成完成：标题=%r 标签=%s 分区=%s",
                     profile.name, result["title"], result["tags"], result["category"] or "（无）")
            return result
        except (ValueError, json.JSONDecodeError) as e:
            last_err = str(e)
            log.warning("[%s] 文案输出解析失败（第 %d 次）：%s", profile.name, attempt, last_err)
            messages.append({"role": "user", "content": f"输出格式错误：{last_err}，请只输出纯 JSON 对象。"})
        except Exception as e:
            last_err = f"调用接口失败：{e}"
            log.warning("[%s] 文案接口异常（第 %d 次）：%s", profile.name, attempt, str(e)[:150])
            time.sleep(2)

    # 兜底：优先复用最后一次成功解析的结果（可能是命中套话的那版），
    # 比抛异常阻断整条流水线好 —— 套话顶多是文案不够出彩，
    # 而 CopywriterError 会让任务卡在 TRANSCRIBED 永远发不出去。
    if last_result:
        log.warning("[%s] 3 次均未通过校验（%s），复用最后一次结果：%r",
                    profile.name, last_err, last_result["title"])
        return last_result
    raise CopywriterError(f"文案生成失败（3 次重试后）：{last_err}")

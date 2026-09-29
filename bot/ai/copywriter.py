"""LLM 文案生成：按平台 profile 生成各平台专属的标题/简介/标签（JSON）。

IP 定位（2026-09 升级）：【无声治愈 / 纯享放松 / 默片搞笑】短视频专栏。
核心要求：
1. 标题固定以 IP 专栏标签开头（【无声治愈】/【无声小剧场】/【纯享放松】），打造极高粉丝辨识度。
2. 严禁凭空编造无中生有的剧情（如做饭带娃写作业）；必须基于原英文/外文 Caption 与画面实际内容拟写。
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

# IP 专栏前缀定义（2026-09-19 起统一为固定前缀【有点视频】，He 指定）
IP_PREFIX = "【有点视频】"
IP_PREFIXES = [IP_PREFIX]

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


PLATFORM_PROFILES: dict[str, PlatformProfile] = {
    "kuaishou": PlatformProfile(
        name="kuaishou", display_name="快手", max_title_len=40, max_tags=4,
        supports_category=True,
        tone="亲切自然、治愈解压、幽默有趣，不用花哨网络黑话",
        desc_hint="1~2 句话点出视频看点或温暖感悟，适合静音观看",
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
        tone="口语、有具体看点。标题含前缀硬限 30 字，前缀占 6 字，钩子只剩 24 字，必须更短",
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

    return f"""你是资深短视频专栏主理人，你的账号定位是【无声治愈 / 纯享放松 / 默片搞笑】精品视频专栏。
特点：视频多为 AI 3D 动画、萌宠小动物剧情、静音幽默小短片，专为想要安静解压、放松心情的读者提供优质视觉内容。

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
1. **固定前缀**：所有标题一律以「【有点视频】」开头，无例外。
2. 前缀之后是一句**夸张、幽默风趣的钩子**，人设是「正在看视频憋笑/震惊，
   恨不得抓着朋友衣袖让他赶紧看」的旁观众。要求：
   - **夸而不假**：可以夸张，但夸张必须建立在**画面真实内容**上，不是空喊；
   - **有具体看点**：钩子里必须能看出「谁 + 干了什么离谱/可爱/过分的事」，
     纯情绪词（「太绝了」「好可爱」单独出现）等于没说；
   - **留悬念**：把最炸的一幕咽住不说，用断点勾人；
   - **口语化**：像弹幕/朋友转述，不像新闻标题；给小动物写拟人内心戏是好招。
   - **每条必须不一样**：主角物种、动作、场景都要照实写。下面这些词因为
     被用烂了，一律禁止出现在标题里：
       走位、翻车、滑跪、脸刹、天衣无缝、王者、帅不过、下一秒、自以为、
       原地、打滑、起飞、六亲不认、残影
     也不要用「它觉得/它以为/本以为」开头的句式（已占 22%）。
     换个说法表达同样的意思，比如不说「翻车」说具体发生了什么。

标题总字数严格不得超过 {profile.max_title_len} 字（含前缀）！

【严禁句式】
严禁出现这些已被用烂的套话：{banned_str}，禁止以「姐妹们你们呢」等俗套口吻结尾。

【话题（tags）—— 固定组合，无需发挥】
话题已按账号数据表现固定（12.5 万爆款同款结构），你只需要原样输出：
  tags 里固定填这 4 个：无声视频、搞笑日常、沙雕视频、搞笑动画
（发布时会自动按平台上限截断/补齐，你照抄即可，不必自己设计话题。）

【输出格式】
只输出一个合法的 JSON 对象，不要输出任何代码块标记、思考过程或多余说明：
{{
  "title": "【有点视频】+ 夸张幽默的钩子（不超过 {profile.max_title_len} 字）",
  "description": "{profile.desc_hint}（1~2句话，真实呼应视频内容）",
  "tags": ["无声视频", "搞笑日常", "沙雕视频", "搞笑动画"],
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

    # 规范化：确保前缀规范
    matched_prefix = None
    for p in IP_PREFIXES:
        if title.startswith(p):
            matched_prefix = p
            break
    if not matched_prefix:
        # 模型漏写括号或用了旧前缀 → 统一归一到【有点视频】
        for clean_p in ("有点视频", "无声治愈", "无声小剧场", "纯享放松"):
            if title.startswith(clean_p) or title.startswith(f"【{clean_p}】"):
                body = title.split("】", 1)[-1] if "】" in title[:12] else title[len(clean_p):]
                title = IP_PREFIX + body
                matched_prefix = IP_PREFIX
                break
        if not matched_prefix:
            title = IP_PREFIX + title
    if not title.startswith(IP_PREFIX):
        title = IP_PREFIX + title.lstrip("【】")

    title = title[:profile.max_title_len]

    description = str(obj.get("description", "")).strip()[:400]
    # 简介不要再抄一遍标题（抖音标题框 + 简介框会叠成重复句子）
    if description.startswith(title):
        description = description[len(title):].lstrip(" \n，。；;:：")
    hook = title[len(IP_PREFIX):].strip() if title.startswith(IP_PREFIX) else ""
    if hook and len(hook) >= 6 and description.startswith(hook):
        description = description[len(hook):].lstrip(" \n，。；;:：")
    tags_raw = obj.get("tags") or []
    if not isinstance(tags_raw, list):
        tags_raw = []
    tags = [str(t).lstrip("#").strip()[:10] for t in tags_raw if str(t).strip()]

    # 基础 IP 标签保障
    if "无声视频" not in tags:
        tags.insert(0, "无声视频")
    tags = [t for t in tags if t]

    # 话题策略（2026-09-19 He 指定）：固定组合
    # 【无声视频 + 搞笑日常 + 沙雕视频 + 搞笑动画/笑到肚子疼】。
    # 依据：12.5 万爆款用的就是 搞笑日常+沙雕视频+无声高能 组合，
    # 搞笑系平均播放碾压纯享系（20,833 vs 2,463）。AI 生成的 tags
    # 不再参与最终输出 —— 固定组合由 enforce_diversity(fixed_tags) 直接返回。
    # 快手 max_tags=4：无声视频+搞笑日常+沙雕视频+搞笑动画
    # 恰好放下；笑到肚子疼 在抖音档（max_tags=5）作为第 5 个加入。
    FIXED_TAGS = ["搞笑日常", "沙雕视频", "搞笑动画", "笑到肚子疼"]
    tags = tag_quality.enforce_diversity(
        tags, profile.max_tags, title=title, base_tag="无声视频",
        fixed_tags=FIXED_TAGS,
    )

    category = str(obj.get("category", "")).strip()
    if profile.supports_category and categories:
        if category not in categories:
            hits = [c for c in categories if category in c or c in category]
            category = hits[0] if hits else categories[0]
    else:
        category = ""

    return {"title": title, "description": description, "tags": tags, "category": category}


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
    "六亲不认", "残影",
]
CLICHE_PREFIXES = ["它觉得", "它以为", "本以为"]


def _check_cliche(title: str) -> list[str]:
    """拦截被用烂的标题套话，返回命中的词表（空表=通过）。"""
    hits = [w for w in CLICHE_WORDS if w in title]
    hook = title[len(IP_PREFIX):].strip() if title.startswith(IP_PREFIX) else title
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
                    f"请重新写：直接说**这个画面里具体发生了什么**"
                    f"（主角是什么动物、在做什么动作、结果怎样），"
                    f"不要用那些套话词。标题：{result['title']}"
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

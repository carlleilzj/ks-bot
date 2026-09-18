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
用户会提供该视频的 Instagram/YouTube 原文案（通常为英文/西语/葡语等外文）和语音转录（可能无声）。
你必须：
1. 仔细阅读并理解原文案的真实含义（包括 emoji、动植物角色、具体事件）。
2. 识别视频的主角（例如小猫、小狗、毛毛虫、小鸟、兔子等）和核心情节（如破茧成蝶、互相取暖、抢食物、失误翻车等）。
3. 严禁无中生有！严禁捏造与原文无关的人类琐事（绝对禁止编造：做饭、带娃、写作业、婆媳、家庭主妇等无关剧情）。

【标题规范（极其重要）】
1. **固定前缀**：所有标题一律以「【有点视频】」开头，无例外。
2. 前缀之后是一句**夸张、幽默风趣的钩子**，人设是「正在看视频憋笑/震惊，
   恨不得抓着朋友衣袖让他赶紧看」的旁观众。要求：
   - **夸而不假**：可以夸张（「我承认我笑得很大声」「这操作我给满分」），
     但夸张必须建立在画面真实内容上，不是空喊；
   - **有具体看点**：钩子里必须能看出「谁 + 干了什么离谱/可爱/过分的事」，
     纯情绪词（「太绝了」「好可爱」单独出现）等于没说；
   - **留悬念**：把最炸的一幕咽住不说 —— 「直到下一秒」「结果它的反应」
     「第 3 秒开始不对劲」这类断点是好东西；
   - **口语化**：像弹幕/朋友转述，不像新闻标题；给小动物写拟人内心戏
     是好招（「它觉得自己藏得很好」）。
   好的例子：
     【有点视频】它觉得自己藏得天衣无缝，尾巴：是吗
     【有点视频】抢玉米抢出残影，下一秒输得毫无尊严
     【有点视频】我发誓这毛毛虫走路比我上班还有仪式感
     【有点视频】装睡装了三分钟，就为等主人走过这一步
     【有点视频】这蛋孵出来的瞬间，我直接原谅了今天所有破事
   差的例子（禁止）：
     【有点视频】安静治愈的画面（纯氛围词，没看点）
     【有点视频】看小动物日常（空洞）
     【有点视频】你会怎么做？（疑问句套话，禁止）
     【有点视频】太解压了吧家人们（空喊情绪，没有内容）

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


def generate_copy(transcript: str, caption: str, categories: list[str], s: Settings,
                  platform: str = "kuaishou") -> dict:
    """生成具备统一【无声/治愈/搞笑】IP 标识并与视频内容高度相关的文案。"""
    profile = PLATFORM_PROFILES.get(platform) or PLATFORM_PROFILES["kuaishou"]
    from .asr import _NOISE_RE
    raw = (transcript or "").strip()
    if raw and _NOISE_RE.search(raw) and len(raw) < 40:
        raw = ""

    user_content = (
        f"【视频来源文案（请重点分析其描述的动植物角色与故事真相）】\n"
        f"{caption.strip() or '（无外文简介，请按无声治愈/纯享短片构思）'}\n\n"
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
    for attempt in range(1, 4):
        try:
            resp = client.chat.completions.create(
                model=s.ai_model, messages=messages, temperature=0.7,
            )
            content = resp.choices[0].message.content or ""
            result = _validate(_extract_json(content), categories, profile)

            banned = _check_hallucination(result["title"], result["description"])
            if banned:
                last_err = "；".join(banned)
                log.warning("[%s] 文案命中禁用词（第 %d 次）：%s", profile.name, attempt, last_err)
                messages.append({"role": "user", "content": f"上次生成违规：{last_err}。请绝对不要出现任何做饭、带娃或俗套套话，只针对原文案角色与事件重新输出。"})
                continue

            log.info("[%s] IP文案生成完成：标题=%r 标签=%s 分区=%s",
                     profile.name, result["title"], result["tags"], result["category"] or "（无）")
            return result
        except (ValueError, json.JSONDecodeError) as e:
            last_err = str(e)
            log.warning("[%s] 文案输出解析失败（第 %d 次）：%s", profile.name, attempt, last_err)
            messages.append({"role": "user", "content": f"输出格式错误：{last_err}，请只输出纯 JSON 对象。"})
        except Exception as e:
            raise CopywriterError(f"调用文案生成接口失败：{e}") from e

    # 兜底
    log.warning("[%s] 3 次生成均触发校验，进行兜底返回", profile.name)
    content = resp.choices[0].message.content or ""
    return _validate(_extract_json(content), categories, profile)

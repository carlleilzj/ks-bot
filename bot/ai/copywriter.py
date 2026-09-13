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

log = logging.getLogger(__name__)

# IP 专栏前缀定义
IP_PREFIXES = ["【无声治愈】", "【无声小剧场】", "【纯享放松】"]

BANNED_PATTERNS = [
    "心里踏实", "真踏实", "心里就踏实", "心里一下子",
    "喘口气", "歇口气",
    "姐妹们", "家人们", "兄弟们",
    "带娃做饭", "做饭带娃", "写作业", "做家务", "做饭",
    "太上头了", "停不下来", "太绝了", "拉满",
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

【标题 IP 规范（极其重要）】
标题必须在 3 个专栏标签中按视频属性选择最契合的一个作为开头：
1. 【无声治愈】—— 适用于温情救助、动物互助、破茧成长、安静陪伴等暖心内容。
   例：【无声治愈】放慢脚步看毛毛虫破茧成蝶，被最后那一幕美到了
   例：【无声治愈】暴风雨里互相依偎的小家伙，看完心里暖暖的
2. 【无声小剧场】—— 适用于幽默滑稽、小动物搞笑走位、争夺食物、意外反转等逗趣内容。
   例：【无声小剧场】两只小家伙抢一根玉米，下一秒直接原地翻车
   例：【无声小剧场】本以为是个身法大师，结果帅不过三秒
3. 【纯享放松】—— 适用于丝滑循环、唯美视效、治愈节律、专注小动作等视觉纯享。
   例：【纯享放松】全程没有一句嘈杂，看它安安静静忙完太解压了

标题总字数严格不得超过 {profile.max_title_len} 字！

【严禁句式】
严禁出现这些已被用烂的套话：{banned_str}，禁止以「姐妹们你们呢」等俗套口吻结尾。

【输出格式】
只输出一个合法的 JSON 对象，不要输出任何代码块标记、思考过程或多余说明：
{{
  "title": "【无声治愈】或【无声小剧场】或【纯享放松】+ 提炼自原文案真实剧情的一句话（不超过 {profile.max_title_len} 字）",
  "description": "{profile.desc_hint}（1~2句话，真实呼应视频内容）",
  "tags": ["无声视频", "治愈", "相关标签（与内容强相关，如小动物/搞笑/动画等）"],
  {category_rule}
}}

{category_req}
- tags 为 3~{profile.max_tags} 个，不带 # 号，每个不超过 10 字。
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
        # 如果模型漏写了括号，尝试补齐
        for clean_p in ["无声治愈", "无声小剧场", "纯享放松"]:
            if title.startswith(clean_p):
                title = f"【{clean_p}】" + title[len(clean_p):]
                matched_prefix = f"【{clean_p}】"
                break
        if not matched_prefix:
            title = f"【无声治愈】{title}"

    title = title[:profile.max_title_len]

    description = str(obj.get("description", "")).strip()[:400]
    tags_raw = obj.get("tags") or []
    if not isinstance(tags_raw, list):
        tags_raw = []
    tags = [str(t).lstrip("#").strip()[:10] for t in tags_raw if str(t).strip()]

    # 基础 IP 标签保障
    if "无声视频" not in tags:
        tags.insert(0, "无声视频")
    tags = [t for t in tags if t][:profile.max_tags]

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

"""LLM 文案生成：按平台 profile 生成各平台专属的标题/简介/标签（JSON）。

每个平台的差异（标题字数上限/分区体系/语气）集中在 PLATFORM_PROFILES。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from ..config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlatformProfile:
    name: str
    display_name: str
    max_title_len: int
    max_tags: int = 5
    supports_category: bool = False
    tone: str = ""              # 语气/风格提示，注入 system prompt
    desc_hint: str = ""         # 简介的额外要求


PLATFORM_PROFILES: dict[str, PlatformProfile] = {
    "kuaishou": PlatformProfile(
        name="kuaishou", display_name="快手", max_title_len=40, max_tags=4,
        supports_category=True,
        tone="语气接地气、口语化，符合快手用户习惯",
        desc_hint="2~3 句话，自然植入关键词",
    ),
    "douyin": PlatformProfile(
        name="douyin", display_name="抖音", max_title_len=55,
        tone="语气年轻化、有梗、节奏快，标题第一句就要抓眼球",
        desc_hint="2~3 句话，简洁有力，结尾自然引出话题标签",
    ),
    "xhs": PlatformProfile(
        name="xhs", display_name="小红书", max_title_len=20,
        tone="语气像朋友真诚分享，标题可加 1~2 个 emoji 增加亲和力，20 字是硬限制必须遵守",
        desc_hint="像笔记正文，口语化、有细节感，可以分点描述",
    ),
    "weixin": PlatformProfile(
        name="weixin", display_name="微信视频号", max_title_len=30,
        tone="语气真诚自然，适合朋友圈传播，标题有信息量但不标题党",
        desc_hint="2~3 句话，简洁明了，结尾可加话题标签",
    ),
}


class CopywriterError(RuntimeError):
    pass


def _system_prompt(profile: PlatformProfile) -> str:
    category_rule = (
        '"category": "必须从给定的分区列表中选择一个"'
        if profile.supports_category else
        '"category": ""（该平台无分区，固定输出空字符串）'
    )
    category_req = (
        "- category 必须严格等于分区列表中的某一项"
        if profile.supports_category else
        "- category 固定输出空字符串"
    )
    return f"""你是资深的{profile.display_name}短视频运营专家，擅长把视频内容包装成吸引点击的中文文案。
用户会给你一段视频的语音转录文本（可能为空）和背景信息。
你的任务是为该视频生成{profile.display_name}发布文案，只输出一个 JSON 对象，不要输出任何其他文字、注释或代码块标记。

JSON 格式：
{{
  "title": "标题，不超过 {profile.max_title_len} 字，有悬念或亮点",
  "description": "作品简介，{profile.desc_hint}",
  "tags": ["话题标签1", "话题标签2", "话题标签3"],
  {category_rule}
}}

要求：
- 全部用简体中文，{profile.tone}
- 不得出现 Instagram、ins、IG、搬运、搬运工、原作者等字样
- 不得出现「字幕by」、字幕作者、水印、制作署名、索兰娅等与画面内容无关的字样
- 若转录像片头片尾水印、无有效对白，按视频常识写文案，不要复述水印
- tags 为 3~{profile.max_tags} 个，不带 # 号，每个不超过 12 字
{category_req}"""


def _client(s: Settings):
    import httpx
    from openai import OpenAI
    if not s.ai_api_key:
        raise CopywriterError("缺少 AI_API_KEY：请在 .env 填写后重试")
    return OpenAI(base_url=s.ai_base_url, api_key=s.ai_api_key,
                  timeout=httpx.Timeout(180, connect=15))


def _extract_json(text: str) -> dict:
    text = text.strip()
    # 剥掉可能的 ```json 代码块
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
    title = str(obj.get("title", "")).strip().strip('"“”')[:profile.max_title_len]
    if not title:
        raise ValueError("title 为空")
    description = str(obj.get("description", "")).strip()[:800]
    tags_raw = obj.get("tags") or []
    if not isinstance(tags_raw, list):
        raise ValueError("tags 不是数组")
    tags = [str(t).lstrip("#").strip()[:12] for t in tags_raw if str(t).strip()]
    tags = [t for t in tags if t][:profile.max_tags]
    category = str(obj.get("category", "")).strip()
    if profile.supports_category:
        if category not in categories:
            # 允许模糊包含匹配，比如"美食"匹配"美食教程"
            hits = [c for c in categories if category in c or c in category]
            if not hits:
                raise ValueError(f"category '{category}' 不在可选列表中")
            category = hits[0]
    else:
        category = ""
    return {"title": title, "description": description, "tags": tags, "category": category}


def generate_copy(transcript: str, caption: str, categories: list[str], s: Settings,
                  platform: str = "kuaishou") -> dict:
    """按平台生成 {title, description, tags, category}。解析/校验失败自动重试一次。"""
    profile = PLATFORM_PROFILES.get(platform) or PLATFORM_PROFILES["kuaishou"]
    from .asr import _NOISE_RE
    raw = (transcript or "").strip()
    if raw and _NOISE_RE.search(raw) and len(raw) < 40:
        raw = ""
    user_content = (
        f"【语音转录文本】\n{raw or '（视频无人声，请根据画面常识创作，不要编造字幕作者）'}\n\n"
        f"【Instagram 原文案】\n{caption.strip() or '（无）'}\n\n"
    )
    if profile.supports_category:
        user_content += f"【{profile.display_name}可选分区列表】\n{'、'.join(categories)}"
    else:
        user_content += f"【发布平台】\n{profile.display_name}（无分区，category 输出空字符串）"
    client = _client(s)
    messages = [
        {"role": "system", "content": _system_prompt(profile)},
        {"role": "user", "content": user_content},
    ]
    last_err = ""
    for attempt in (1, 2):
        try:
            resp = client.chat.completions.create(
                model=s.ai_model, messages=messages, temperature=0.7,
            )
            content = resp.choices[0].message.content or ""
            result = _validate(_extract_json(content), categories, profile)
            log.info("[%s] 文案生成完成：标题=%r 分区=%s 标签=%s",
                     profile.name, result["title"], result["category"] or "（无）", result["tags"])
            return result
        except (ValueError, json.JSONDecodeError) as e:
            last_err = str(e)
            log.warning("[%s] 文案输出不合规（第 %d 次）：%s", profile.name, attempt, last_err)
            messages.append({"role": "user",
                             "content": f"你上次的输出不合规：{last_err}。请重新只输出一个合法 JSON。"})
        except Exception as e:
            raise CopywriterError(f"调用文案生成接口失败：{e}") from e
    raise CopywriterError(f"[{profile.name}] 文案生成两次均不合规：{last_err}")

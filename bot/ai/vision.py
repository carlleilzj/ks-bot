"""封面 AI 质检：用 vision 模型判断封面是否含真人、有无水印、是否动画内容。

发现层用它做反向过滤（动画赛道：真人丢弃）+ 水印过滤 + 赛道匹配。
一次 API 调用同时返回三个判定，省调用次数。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings

log = logging.getLogger(__name__)


class RealPersonError(RuntimeError):
    pass


@dataclass
class CoverVerdict:
    """封面质检结论。is_animation/has_real_person/has_watermark 任一不确定时为 None。"""

    is_animation: bool | None = None      # True=动画/3D渲染/AI生成角色
    has_real_person: bool | None = None   # True=含真人（真实人类面孔/身体）
    has_watermark: bool | None = None     # True=有水印/平台logo/字幕组标记
    watermark_desc: str = ""              # 水印描述（位置/内容）
    reason: str = ""                      # 综合说明

    @property
    def ok_for_animal_anime(self) -> bool:
        """动画赛道的准入判定：必须是动画、不含真人、无水印。不确定时放行（人工把关）。"""
        if self.has_real_person is True:
            return False
        if self.has_watermark is True:
            return False
        if self.is_animation is False:   # 明确不是动画（真人实拍等）才拒
            return False
        return True


def check_real_person(video: Path | None, cover: Path, s: Settings) -> bool:
    """检测视频是否含真人镜头。返回 True 表示含真人（应跳过），False 表示可发布。

    兼容旧接口：发现层对未下载的候选传 video=None。
    新代码建议直接用 inspect_cover() 拿完整判定。
    """
    return _check_real_person_compat(video, cover, s)


def _check_real_person_compat(video: Path | None, cover: Path, s: Settings) -> bool:
    """旧接口实现：只看 has_real_person 字段。"""
    if not cover.exists():
        return False
    try:
        verdict = inspect_cover(cover, s)
        return verdict.has_real_person is True
    except Exception as e:
        log.warning("AI 视觉检测失败（%s），默认放行（不跳过）", e)
        return False


def inspect_cover(cover: Path, s: Settings) -> CoverVerdict:
    """封面综合质检：一次调用返回 真人/水印/是否动画 三个判定。

    调用失败抛异常（调用方决定放行策略）；JSON 解析失败返回不确定的 verdict。
    """
    if not cover.exists():
        raise FileNotFoundError(f"封面不存在: {cover}")

    import httpx
    from openai import OpenAI

    client = OpenAI(base_url=s.ai_base_url,
                    api_key=s.vision_api_key or s.ai_api_key,
                    timeout=httpx.Timeout(60, connect=15))

    img_b64 = base64.b64encode(cover.read_bytes()).decode("utf-8")
    mime = "image/jpeg"

    # 候选模型：VISION_MODEL 优先，其后主模型 + 常见 vision 模型名兜底
    vision_candidates = []
    if s.vision_model:
        vision_candidates.append(s.vision_model)
    if s.ai_model and s.ai_model not in vision_candidates:
        vision_candidates.append(s.ai_model)
    for fallback in ("gpt-4o-mini", "gemini-2.5-flash", "glm-4v-flash"):
        if fallback not in vision_candidates:
            vision_candidates.append(fallback)

    prompt = (
        "这是一段视频的封面帧。请一次判定三件事，只回答JSON：\n"
        '1. "is_animation": 画面是否为动画/3D渲染/AI生成角色/卡通/插画（true/false）\n'
        '2. "has_real_person": 是否含真人（真实人类面孔或真人身体；动画角色不算）(true/false)\n'
        '3. "has_watermark": 画面是否有水印（平台logo如TikTok/抖音/水印文字/字幕组标记/'
        "频道名角标/时间戳；注意画面内的剧情道具文字不算）(true/false)\n"
        '4. "watermark_desc": 水印内容与位置简述，没有则空字符串\n'
        '5. "reason": 综合简述\n\n'
        '示例：{"is_animation": true, "has_real_person": false, '
        '"has_watermark": true, "watermark_desc": "右下角TikTok logo", "reason": "动画但带平台水印"}'
    )

    last_err = ""
    for model in vision_candidates:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一个图片内容审核助手。只回答 JSON。"},
                    {"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                    ]},
                ],
                max_tokens=150,
            )
            content = resp.choices[0].message.content or ""
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE).strip()
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if not m:
                last_err = f"模型 {model} 返回非JSON: {content[:100]}"
                continue
            data = json.loads(m.group(0))
            verdict = CoverVerdict(
                is_animation=bool(data["is_animation"]) if "is_animation" in data else None,
                has_real_person=bool(data["has_real_person"]) if "has_real_person" in data else None,
                has_watermark=bool(data["has_watermark"]) if "has_watermark" in data else None,
                watermark_desc=str(data.get("watermark_desc", ""))[:100],
                reason=str(data.get("reason", ""))[:200],
            )
            log.info("封面质检（模型=%s）：animation=%s person=%s watermark=%s %s",
                     model, verdict.is_animation, verdict.has_real_person,
                     verdict.has_watermark, verdict.watermark_desc)
            return verdict
        except Exception as e:
            last_err = f"模型 {model}: {str(e)[:100]}"
            continue

    log.warning("所有 vision 模型均不可用：%s。返回不确定判定。", last_err)
    return CoverVerdict(reason=f"检测失败: {last_err[:150]}")

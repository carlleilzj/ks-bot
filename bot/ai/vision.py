"""真人镜头检测：从视频抽帧，用 AI 视觉模型判断是否含真人镜头。"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from ..config import Settings

log = logging.getLogger(__name__)


class RealPersonError(RuntimeError):
    pass


def check_real_person(video: Path | None, cover: Path, s: Settings) -> bool:
    """检测视频是否含真人镜头。返回 True 表示含真人（应跳过），False 表示可发布。

    策略：用 cover 帧图 + 视频中间帧，发给 AI 视觉模型判断。
    AI 视觉走 AI_BASE_URL 配置的 OpenAI 兼容接口（需要模型支持 vision）。
    video 参数当前未使用（只看封面帧）；发现层对未下载的候选传 None。
    """
    if not cover.exists():
        return False

    # 尝试用 AI 视觉模型判断
    try:
        return _check_via_vision_api(cover, s)
    except Exception as e:
        log.warning("AI 视觉检测失败（%s），默认放行（不跳过）", e)
        return False


def _check_via_vision_api(image: Path, s: Settings) -> bool:
    """用 OpenAI 兼容的 vision API 判断图片是否含真人。"""
    import httpx
    from openai import OpenAI

    client = OpenAI(base_url=s.ai_base_url, api_key=s.ai_api_key,
                    timeout=httpx.Timeout(60, connect=15))

    # 把图片转 base64
    img_b64 = base64.b64encode(image.read_bytes()).decode("utf-8")
    mime = "image/jpeg"

    # 尝试用 vision 模型（需要模型名带 vision 能力）。
    # 注意：grok 全系（含 grok-4.5/4.6）不接受 image_url 输入，会 400/404；
    # 若 AI_BASE_URL 中转站只有 grok，真人检测会走"默认放行"分支。
    # 支持图输入的常见模型：gpt-4o / gemini-2.x-flash / glm-4v / claude-3.5+
    vision_candidates = [s.ai_model, "gpt-4o-mini", "gpt-4o",
                          "gemini-2.0-flash", "glm-4v-flash"]

    last_err = ""
    for model in vision_candidates:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一个图片内容审核助手。只回答 JSON。"},
                    {"role": "user", "content": [
                        {"type": "text", "text": "这是一段视频的封面帧。请判断这个视频是否包含真人（真实人类面孔或真人身体镜头）。注意：动画、3D渲染、AI生成角色、卡通、插画都不算真人。只回答JSON：{\"has_real_person\": true/false, \"reason\": \"简短说明\"}"},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                    ]},
                ],
                max_tokens=100,
            )
            content = resp.choices[0].message.content or ""
            # 解析 JSON
            import json
            import re
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE).strip()
            m = re.search(r'\{.*\}', content, re.DOTALL)
            if not m:
                last_err = f"模型 {model} 返回非JSON: {content[:100]}"
                continue
            data = json.loads(m.group(0))
            has_real = bool(data.get("has_real_person", False))
            reason = data.get("reason", "")
            log.info("真人检测（模型=%s）：has_real_person=%s, reason=%s", model, has_real, reason)
            return has_real
        except Exception as e:
            last_err = f"模型 {model}: {str(e)[:100]}"
            continue

    log.warning("所有 vision 模型均不可用：%s。默认放行。", last_err)
    return False

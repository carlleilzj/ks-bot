"""封面 AI 质检：用 vision 模型判断封面是否含真人、有无水印、是否动画内容。

发现层用它做反向过滤（动画赛道：真人丢弃）+ 水印过滤 + 赛道匹配。
一次 API 调用同时返回多个判定，省调用次数。

## 真人判定的演进（2026-09-15 修正）

旧逻辑用 `has_real_person` 做**存在性**判断：只要帧内任何位置（含虚化背景、
远处路人、甚至 3D 建模的远景人物）出现人形就一票否决整条任务。
实测误杀了 `instagram_Dc_obVBP4ZP`（纯 3D 动画短剧，6 帧抽检全为动画角色，
但片头 1 秒背景有个虚化人形轮廓）→ 整条被 SKIPPED。

现改为**三层判定**，解决误杀：
1. `is_animation=True` → 真人规则整体不适用（动画里的"人"是建模角色，不是真人）；
2. 真人有**主体占比**（`real_person_ratio`）与**是否主体**（`real_person_is_subject`）
   两个辅助字段，只有占比超阈值或明确为主体时才否决；
3. 多帧采样（由调用方 `inspect_video_frames` 完成），任一帧判为动画即视为动画内容。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings

log = logging.getLogger(__name__)

# 真人占比准入阈值：画面中真人像素/面积占比超过该值才否决。
# 0.15 的含义：真人需占据画面约 1/6 以上才视为"真人出镜内容"；
# 背景虚化路人（通常 <0.05）、远景 3D 人物建模不会被误杀。
REAL_PERSON_RATIO_THRESHOLD = 0.15


class RealPersonError(RuntimeError):
    pass


@dataclass
class CoverVerdict:
    """封面质检结论。各 bool 字段不确定时为 None。"""

    is_animation: bool | None = None      # True=动画/3D渲染/AI生成角色
    has_real_person: bool | None = None   # True=画面出现真实人类（含背景路人）
    has_watermark: bool | None = None     # True=有水印/平台logo/字幕组标记
    watermark_desc: str = ""              # 水印描述（位置/内容）
    reason: str = ""                      # 综合说明
    # --- 2026-09-15 新增：真人占比判定辅助字段 ---
    real_person_ratio: float | None = None      # 真人在画面中的面积占比 0.0~1.0
    real_person_is_subject: bool | None = None  # 真人是否为画面主体
    real_person_desc: str = ""                  # 真人描述（位置/性质）

    @property
    def real_person_blocks(self) -> bool:
        """真人是否构成否决条件。

        规则（按优先级）：
        1. 判定为动画内容 → 不否决（动画中的人形是建模角色）；
        2. 明确标记为主体 → 否决；
        3. 占比 >= 阈值 → 否决；
        4. 占比未知（旧模型未返回该字段）时，退化为保守否决，
           保证旧接口行为不变。
        """
        if self.is_animation is True:
            return False
        if self.has_real_person is not True:
            return False
        if self.real_person_is_subject is True:
            return True
        if self.real_person_ratio is not None:
            return self.real_person_ratio >= REAL_PERSON_RATIO_THRESHOLD
        return True  # 占比未知：保守否决（兼容旧模型/旧测试）

    @property
    def ok_for_animal_anime(self) -> bool:
        """动画赛道的准入判定：不含真人主体、无水印、非明确实拍。

        不确定时放行（人工把关）。
        """
        if self.real_person_blocks:
            return False
        if self.has_watermark is True:
            return False
        if self.is_animation is False:   # 明确不是动画（真人实拍等）才拒
            return False
        return True

    @property
    def reject_reason(self) -> str:
        """人类可读的否决原因（用于 SKIPPED error 字段与日志）。"""
        if self.real_person_blocks:
            if self.real_person_ratio is not None:
                return f"真人镜头（占比 {self.real_person_ratio:.0%}）"
            return "真人镜头"
        if self.has_watermark is True:
            return f"水印: {self.watermark_desc}" if self.watermark_desc else "水印"
        if self.is_animation is False:
            return "非动画内容"
        return ""


def watermark_only_block(verdict: CoverVerdict) -> bool:
    """判定「否决原因仅为水印」——即真人规则与动画规则都不构成否决。

    用于 step_transcode：若已执行 delogo 擦水印，模型会把插值填充痕迹
    识别为"半透明模糊/马赛克水印痕迹"（我们自己的处理痕迹，非平台水印），
    此时不应再因水印规则二次否决整条任务。
    """
    return (verdict.has_watermark is True
            and not verdict.real_person_blocks
            and verdict.is_animation is not False)


def check_real_person(video: Path | None, cover: Path, s: Settings) -> bool:
    """检测视频是否含真人镜头。返回 True 表示含真人（应跳过），False 表示可发布。

    兼容旧接口：发现层对未下载的候选传 video=None。
    新代码建议直接用 inspect_cover() 拿完整判定。
    """
    return _check_real_person_compat(video, cover, s)


def _check_real_person_compat(video: Path | None, cover: Path, s: Settings) -> bool:
    """旧接口实现：只看真人判定（已含动画豁免与占比阈值）。"""
    if not cover.exists():
        return False
    try:
        verdict = inspect_cover(cover, s)
        return verdict.real_person_blocks
    except Exception as e:
        log.warning("AI 视觉检测失败（%s），默认放行（不跳过）", e)
        return False


def _vision_candidates(s: Settings) -> list[str]:
    """候选模型：VISION_MODEL 优先，其后主模型 + 常见 vision 模型名兜底。"""
    out: list[str] = []
    if s.vision_model:
        out.append(s.vision_model)
    if s.ai_model and s.ai_model not in out:
        out.append(s.ai_model)
    for fallback in ("gpt-4o-mini", "gemini-2.5-flash", "glm-4v-flash"):
        if fallback not in out:
            out.append(fallback)
    return out


_PROMPT = (
    "这是一段视频的某一帧画面（可能是动画片）。请判定以下字段，只回答 JSON：\n"
    '1. "is_animation": 这一帧是否为动画 / 3D 渲染 / AI 生成 / 卡通 / 插画风格（true/false）\n'
    '2. "has_real_person": 画面中是否出现**实拍真人**。'
    "判定要点（务必严格遵守）：\n"
    "   - 3D 建模 / CG 渲染 / 动画风格的人物角色 → **不算真人**，填 false；\n"
    "   - 动画场景中的远景人物、虚化人物建模 → **不算真人**，填 false；\n"
    "   - 只有清晰可辨的实拍人类（真实皮肤纹理、真实五官、纪录片/自拍/Vlog 质感）才算 true；\n"
    "   - 若整帧是动画风格，本字段一律填 false。\n"
    '3. "real_person_ratio": 画面中实拍真人占据的面积比例，0.0~1.0 的浮点数；'
    "没有实拍真人则填 0.0（例如背景里站着个很小的路人填 0.05）\n"
    '4. "real_person_is_subject": 实拍真人是否为画面主体/主角（true/false）；'
    "没有实拍真人填 false\n"
    '5. "real_person_desc": 实拍真人的位置与性质简述，没有则空字符串\n'
    '6. "has_watermark": 画面是否有水印（平台logo如TikTok/抖音/水印文字/字幕组标记/'
    "频道名角标/时间戳；画面内的剧情道具文字不算）(true/false)\n"
    '7. "watermark_desc": 水印内容与位置简述，没有则空字符串\n'
    '8. "reason": 综合简述\n\n'
    "示例1（3D 动画短剧，背景有虚化人物建模）："
    '{"is_animation": true, "has_real_person": false, "real_person_ratio": 0.0, '
    '"real_person_is_subject": false, "real_person_desc": "", '
    '"has_watermark": true, "watermark_desc": "左上角圆形Logo，右下角白色文字账号名", '
    '"reason": "3D动画场景，人物均为建模角色"}\n'
    "示例2（真人 Vlog）："
    '{"is_animation": false, "has_real_person": true, "real_person_ratio": 0.65, '
    '"real_person_is_subject": true, "real_person_desc": "画面中央女性主播，占据主要画面", '
    '"has_watermark": false, "watermark_desc": "", "reason": "真人出镜Vlog"}'
)


def _parse_ratio(raw) -> float | None:
    """稳健解析占比字段（模型可能返回 0.05 / "5%" / "0.05" 等形式）。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
    else:
        s = str(raw).strip()
        if not s:
            return None
        pct = s.endswith("%")
        s = s.rstrip("%").strip()
        try:
            val = float(s)
        except ValueError:
            return None
        if pct:
            val = val / 100.0
    if val > 1.0:            # 模型给了 5 而不是 0.05 这类情况
        val = val / 100.0
    return max(0.0, min(1.0, val))


def inspect_cover(cover: Path, s: Settings) -> CoverVerdict:
    """封面综合质检：一次调用返回 动画/真人/水印 等判定。

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

    last_err = ""
    for model in _vision_candidates(s):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一个图片内容审核助手。只回答 JSON。"},
                    {"role": "user", "content": [
                        {"type": "text", "text": _PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                    ]},
                ],
                max_tokens=300,
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
                real_person_ratio=_parse_ratio(data.get("real_person_ratio")),
                real_person_is_subject=(bool(data["real_person_is_subject"])
                                        if "real_person_is_subject" in data else None),
                real_person_desc=str(data.get("real_person_desc", ""))[:120],
            )
            log.info("封面质检（模型=%s）：animation=%s person=%s ratio=%s subject=%s "
                     "watermark=%s %s",
                     model, verdict.is_animation, verdict.has_real_person,
                     verdict.real_person_ratio, verdict.real_person_is_subject,
                     verdict.has_watermark, verdict.watermark_desc)
            return verdict
        except Exception as e:
            last_err = f"模型 {model}: {str(e)[:100]}"
            continue

    log.warning("所有 vision 模型均不可用：%s。返回不确定判定。", last_err)
    return CoverVerdict(reason=f"检测失败: {last_err[:150]}")


def inspect_video_frames(frames: list[Path], s: Settings) -> CoverVerdict:
    """多帧质检：抽 N 帧分别判定后综合，避免单帧误判。

    综合规则：
    - 任一帧判定为动画（is_animation=True）→ 整片按动画处理（真人规则不适用）；
    - 多帧中多数帧判定真人主体 → 否决；
    - 任一帧有水印 → 标记有水印（水印通常全片固定，命中即可信）；
    - 全部帧检测失败 → 返回不确定判定（调用方放行，人工把关）。
    """
    verdicts: list[CoverVerdict] = []
    for f in frames:
        if not f.exists():
            continue
        try:
            verdicts.append(inspect_cover(f, s))
        except Exception as e:
            log.warning("多帧质检：%s 检测失败（%s）", f.name, str(e)[:100])

    if not verdicts:
        return CoverVerdict(reason="多帧质检：全部帧检测失败")

    # 1) 动画优先：任一帧为动画即视为动画内容
    any_animation = any(v.is_animation is True for v in verdicts)
    # 2) 水印：任一帧命中即可信
    wm = next((v for v in verdicts if v.has_watermark is True), None)
    # 3) 真人：取占比最高的一帧代表全片（主体通常出现在高光帧）
    person_frames = [v for v in verdicts if v.has_real_person is True]
    worst_person = max(
        person_frames,
        key=lambda v: (v.real_person_ratio if v.real_person_ratio is not None else 1.0),
    ) if person_frames else None

    merged = CoverVerdict(
        is_animation=True if any_animation else
                     (False if all(v.is_animation is False for v in verdicts) else None),
        has_real_person=True if worst_person else
                         (False if all(v.has_real_person is False for v in verdicts) else None),
        has_watermark=True if wm else
                       (False if all(v.has_watermark is False for v in verdicts) else None),
        watermark_desc=wm.watermark_desc if wm else "",
        real_person_ratio=worst_person.real_person_ratio if worst_person else None,
        real_person_is_subject=worst_person.real_person_is_subject if worst_person else None,
        real_person_desc=worst_person.real_person_desc if worst_person else "",
        reason=f"多帧综合（{len(verdicts)} 帧）",
    )
    log.info("多帧质检（%d 帧）：animation=%s person=%s ratio=%s subject=%s watermark=%s",
             len(verdicts), merged.is_animation, merged.has_real_person,
             merged.real_person_ratio, merged.real_person_is_subject, merged.has_watermark)
    return merged

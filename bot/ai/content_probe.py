"""视频内容探针：抽帧 + vision 识别，把画面真实内容喂给文案生成。

背景（2026-09-27 实测）：发布的作品标题与内容牛头不对马嘴。
根因是 step_copywrite 把 caption 硬编码为空串、只传 transcript，而素材
92% 是无声视频 —— 模型拿到零输入，只能把 system prompt 里的示例词
（走位/翻车/滑跪/天衣无缝）重新排列组合，20+ 条标题一个模子。

实测对照（vision 看画面 vs 已发布标题）：
  instagram_DdqB9j4SotD  画面=蜥蜴与小蛇在丛林水面竞速狂奔
                          标题=「蹑手蹑脚演了半天，下一秒原地翻车」
  youtube_kWhZRahpgXE    画面=企鹅/蚂蚁/螃蟹团队协作抵御威胁
                          标题=「王者出场，下一秒把自己绊成球」
两条的源 caption 里都写着真实剧情（Cross a Bridge / How Animals Work In Team），
但一个字都没进 prompt。

本模块让文案先"看"一遍画面：均匀抽帧 → 一次 vision 调用 → 结构化摘要。
摘要落盘缓存（work_dir/{sc}_content.json），重跑不重复调用。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

from ..config import Settings
from ..media import ffmpeg

log = logging.getLogger(__name__)

# 抽帧数量：覆盖 5%~95% 时间轴。太少看不出情节，太多浪费 token。
N_FRAMES = 6

_PROMPT = (
    "这是同一条短视频按时间顺序均匀抽取的 {n} 帧"
    "（第 1 张约在 5% 处，最后 1 张约在 95% 处）。\n\n"
    "请只描述**画面里真实发生的事**，不要臆测、不要补全剧情。"
    "全部用中文回答，只输出一个 JSON 对象：\n"
    '{{"subject": "主角是什么（具体物种/角色 + 外貌特征，'
    '如「背部长橙色棘刺的绿色小蜥蜴」）",'
    ' "action": "主角在做什么（具体动作）",'
    ' "setting": "场景环境",'
    ' "beats": "按时间顺序的关键情节（一句话，说清谁做了什么）",'
    ' "summary": "一句话概括看点（20~40 字，具体到谁做了什么，不要形容词堆砌）",'
    ' "is_animation": 整段视频是否为动画/3D 渲染/CG/AI 生成/卡通（true/false），'
    ' "has_real_person": 画面中是否出现**实拍真人**'
    '（真实皮肤纹理与五官；3D 建模人物、动画角色一律 false）（true/false）}}'
)


def is_real_person_footage(content: dict) -> bool:
    """探针结果是否指向实拍真人素材（非动画）。

    背景：vision.real_person_check 自 2026-09-16 起为 false（手动投链模式关掉的），
    于是真人素材不再被拦。实测 task 241 是一条真人婴儿抚触视频混进了待发队列，
    它既不符合账号定位，还会触发上游内容审核（文案接口返回空 choices）。
    这里用探针的 is_animation/has_real_person 字段做一道兜底判定。

    只认明确的判定：字段缺失或为 None 时返回 False（不误杀）。
    """
    if not content:
        return False
    if content.get("is_animation") is True:
        return False
    return content.get("has_real_person") is True


def _extract(video: Path, n: int, work_dir: Path, sc: str) -> list[Path]:
    """在 5%~95% 均匀抽 n 帧。失败的跳过，全失败返回空表。"""
    try:
        info = ffmpeg.video_info(video)
        dur = float(info.get("duration") or 0)
    except Exception as e:
        log.warning("[%s] 内容探针：读取时长失败：%s", sc, str(e)[:80])
        return []
    if dur <= 0:
        return []

    lo, hi = dur * 0.05, dur * 0.95
    step = (hi - lo) / max(n - 1, 1)
    out: list[Path] = []
    for i in range(n):
        t = lo + step * i
        f = work_dir / f"{sc}_probe{i}.jpg"
        if not f.exists():
            try:
                ffmpeg._run([  # noqa: SLF001 —— 复用模块内 ffmpeg 调用
                    ffmpeg._ffmpeg(), "-y", "-ss", f"{t:.2f}", "-i", str(video),  # noqa: SLF001
                    "-frames:v", "1", "-q:v", "3", str(f),
                ], timeout=120)
            except Exception as e:
                log.debug("[%s] 探针抽帧失败 t=%.1fs：%s", sc, t, str(e)[:80])
                continue
        if f.exists():
            out.append(f)
    return out


def _ask_vision(frames: list[Path], s: Settings) -> dict:
    """一次 vision 调用描述全部帧。失败抛异常，由调用方兜底。"""
    import httpx
    from openai import OpenAI

    from .vision import _vision_candidates

    client = OpenAI(base_url=s.ai_base_url,
                    api_key=s.vision_api_key or s.ai_api_key,
                    timeout=httpx.Timeout(150, connect=15))
    content: list[dict] = [{"type": "text", "text": _PROMPT.format(n=len(frames))}]
    for f in frames:
        b64 = base64.b64encode(f.read_bytes()).decode("utf-8")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    last_err = ""
    for model in _vision_candidates(s):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是视频内容分析助手。只回答 JSON。"},
                    {"role": "user", "content": content},
                ],
                max_tokens=600,
            )
            raw = (resp.choices[0].message.content or "").strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw,
                         flags=re.IGNORECASE).strip()
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                last_err = f"模型 {model} 返回非 JSON：{raw[:120]}"
                continue
            data = json.loads(m.group(0))
            if not isinstance(data, dict):
                last_err = f"模型 {model} 返回的不是对象"
                continue
            data["model"] = model
            return data
        except Exception as e:
            last_err = f"模型 {model}: {str(e)[:120]}"
            continue
    raise RuntimeError(f"vision 全部失败：{last_err[:200]}")


def describe_video(video: Path, s: Settings, work_dir: Path, sc: str,
                   n: int = N_FRAMES, refresh: bool = False) -> dict:
    """抽帧 + vision 识别，返回 {subject, action, setting, beats, summary, model}。

    任何失败都返回 {} —— 文案生成会退化为「只有源文案 + 转录」的旧行为，
    绝不因为探针失败挡住发布。结果缓存到 work_dir/{sc}_content.json。
    """
    cache = work_dir / f"{sc}_content.json"
    if cache.exists() and not refresh:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("summary"):
                log.info("[%s] 内容探针命中缓存：%s", sc, str(data.get("summary"))[:80])
                return data
        except Exception:
            pass

    if not video.exists():
        log.warning("[%s] 内容探针：成片不存在 %s", sc, video)
        return {}

    frames = _extract(video, n, work_dir, sc)
    if not frames:
        log.warning("[%s] 内容探针：抽帧全部失败", sc)
        return {}

    try:
        data = _ask_vision(frames, s)
    except Exception as e:
        log.warning("[%s] 内容探针：%s", sc, str(e)[:200])
        return {}

    try:
        cache.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    except Exception:
        pass
    log.info("[%s] 内容探针完成（%d 帧，模型=%s）：%s", sc, len(frames),
             data.get("model", "?"), str(data.get("summary", ""))[:100])
    return data

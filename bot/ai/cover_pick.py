"""封面自动选优：抽多候选帧 → vision 打分 → 选最佳当封面。

旧机制（2026-09-19 前）：ffmpeg 在第 1 秒盲截一帧，片头平淡封面就废。
新机制：转码完成后均匀抽 6 帧 → 一次 vision 调用给全部帧打分 →
取综合分最高的帧写为封面。

打分维度（让 vision 按 0~10 打）：
- 主体突出：主角（动物/角色）清晰、占画面比例合适
- 视觉冲击：色彩、光影、构图有没有「停下来看」的吸引力
- 信息量：能看出视频主题（在吃什么/在哪/发生了什么）

容错链：候选帧抽帧失败→用已有帧；全失败→退回旧的第 1 秒截帧；
vision 全挂→退回第一张成功帧；任何异常都不阻塞发布主流程。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

from ..media import ffmpeg

log = logging.getLogger(__name__)

# 候选帧数量：6 帧覆盖 10%~90% 时间轴，兼顾信息与调用成本
N_CANDIDATES = 6

_SCORE_PROMPT = """你是短视频封面评审。下面是同一条视频按时间顺序均匀抽取的 %d 帧（第 1 张对应约 10%% 处，最后 1 张约 90%% 处）。

请给每一帧打封面适用分（0~10，可一位小数），三个维度各占 1/3：
1. 主体突出：画面主角（动物/角色/核心物体）清晰可辨、大小合适、不被遮挡；
2. 视觉冲击：色彩、光影、构图是否让人「刷到就想点」；
3. 信息量：能否一眼看出视频主题（在吃什么/在哪/正在发生什么）。

同时报告每帧是否含真人（has_real_person，布尔）。

只输出一个 JSON 对象，不要代码块标记：
{"frames": [{"index": 1, "score": 7.5, "has_real_person": false, "why": "主角特写清晰"},
            {"index": 2, "score": 4.0, "has_real_person": false, "why": "远景主体太小"}]}
index 从 1 开始，按图片顺序。"""


def _extract_candidates(video: Path, work_dir: Path, sc: str) -> list[Path]:
    """在视频 10%~90% 均匀抽 N 帧。失败的帧跳过，全失败返回空表。"""
    try:
        info = ffmpeg.video_info(video)
        duration = float(info.get("duration") or 0)
    except Exception as e:
        log.warning("[%s] 候选帧：读取时长失败：%s", sc, str(e)[:80])
        return []
    if duration <= 0:
        return []

    lo, hi = duration * 0.10, duration * 0.90
    step = (hi - lo) / max(N_CANDIDATES - 1, 1)
    frames: list[Path] = []
    for i in range(N_CANDIDATES):
        t = lo + step * i
        f = work_dir / f"{sc}_cand{i}.jpg"
        if f.exists():
            frames.append(f)
            continue
        try:
            ffmpeg._run([  # noqa: SLF001 —— 复用模块内 ffmpeg 调用
                ffmpeg._ffmpeg(), "-y", "-ss", f"{t:.2f}", "-i", str(video),
                "-frames:v", "1", "-q:v", "2", str(f),
            ], timeout=120)
            if f.exists():
                frames.append(f)
        except Exception as e:
            log.debug("[%s] 候选帧 t=%.1fs 抽取失败：%s", sc, t, str(e)[:80])
    return frames


def _vision_rank(frames: list[Path], s) -> list[tuple[Path, float, bool]] | None:
    """一次 vision 调用给全部候选帧打分。

    返回 [(frame, score, has_real_person)] 按分降序；全部模型失败返回 None。
    """
    if not frames:
        return None

    import httpx
    from openai import OpenAI

    from .vision import _vision_candidates  # noqa: PLC0415 —— 复用模型候选链
    client = OpenAI(base_url=s.ai_base_url,
                    api_key=s.vision_api_key or s.ai_api_key,
                    timeout=httpx.Timeout(90, connect=15))

    content: list[dict] = [{"type": "text", "text": _SCORE_PROMPT % len(frames)}]
    for f in frames:
        b64 = base64.b64encode(f.read_bytes()).decode("utf-8")
        content.append({"type": "text", "text": f"第 {len(content)} 张（帧 {len(content)}）："})
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    last_err = ""
    for model in _vision_candidates(s):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是图片评审助手。只回答 JSON。"},
                    {"role": "user", "content": content},
                ],
                max_tokens=600,
            )
            raw = resp.choices[0].message.content or ""
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw,
                         flags=re.IGNORECASE).strip()
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                last_err = f"模型 {model} 返回非JSON"
                continue
            data = json.loads(m.group(0))
            items = data.get("frames") or []
            out: list[tuple[Path, float, bool]] = []
            for it in items:
                try:
                    idx = int(it.get("index", 0))
                except (TypeError, ValueError):
                    continue
                if not (1 <= idx <= len(frames)):
                    continue
                try:
                    score = float(it.get("score", 0))
                except (TypeError, ValueError):
                    score = 0.0
                out.append((frames[idx - 1], score,
                            bool(it.get("has_real_person", False))))
            if out:
                out.sort(key=lambda x: -x[1])
                log.info("封面选优完成（模型=%s）：最佳 %s（%.1f 分）",
                         model, out[0][0].name, out[0][1])
                return out
            last_err = f"模型 {model} frames 为空"
        except Exception as e:
            last_err = f"模型 {model}: {str(e)[:100]}"
            continue
    log.warning("封面选优 vision 全部失败：%s", last_err[:150])
    return None


def pick_best_cover(video: Path, work_dir: Path, sc: str,
                    out_path: Path, s) -> Path:
    """主入口：选优封面写到 out_path。

    降级链：候选帧抽取失败 → 第 1 秒截帧（旧行为）；
    vision 打分失败 → 候选帧里取中间帧（25% 处比片头信息量大）。
    永不抛异常 —— 封面选优失败不能挡住发布。
    """
    try:
        frames = _extract_candidates(video, work_dir, sc)
        if not frames:
            # 完全抽不出候选帧 → 旧行为兜底
            ffmpeg.extract_cover(video, out_path, at=1.0)
            return out_path

        ranked = _vision_rank(frames, s)
        if ranked:
            best, score, _has_person = ranked[0]
            out_path.write_bytes(best.read_bytes())
            log.info("[%s] 封面选定：%s（%.1f 分，共 %d 候选）",
                     sc, best.name, score, len(frames))
        else:
            # vision 挂了 → 取 25% 处那帧（通常已进正片，比片头强）
            mid = frames[min(1, len(frames) - 1)]
            out_path.write_bytes(mid.read_bytes())
            log.info("[%s] vision 不可用，封面退化为候选第 2 帧", sc)
        return out_path
    except Exception as e:
        # 最后兜底：旧行为，再失败就让调用方走原 extract_cover 的异常路径
        log.warning("[%s] 封面选优异常（退回第 1 秒截帧）：%s", sc, str(e)[:120])
        try:
            ffmpeg.extract_cover(video, out_path, at=1.0)
        except Exception:
            pass
        return out_path

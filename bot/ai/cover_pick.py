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

# 封面「过暗」阈值：比 ffmpeg.is_black_frame 的 18 宽——夜晚/森林素材常有
# luma 20~26 的近黑帧，过黑帧检测但发出去就是黑封面（2026-10-25 实测有
# 一条黑屏封面上线）。封面专用此更严阈值，帧抽取的黑帧过滤维持 18 不变。
DARK_COVER_LUMA = 26.0

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
            # 缓存帧同样过黑帧检测：重试任务会复用旧候选文件，
            # 不复查的话旧黑帧会混进候选（2026-10-25 补漏）
            if not ffmpeg.is_black_frame(f):
                frames.append(f)
            else:
                log.debug("[%s] 缓存候选帧 t=%.1fs 是黑帧，跳过", sc, t)
            continue
        try:
            ffmpeg._run([  # noqa: SLF001 —— 复用模块内 ffmpeg 调用
                ffmpeg._ffmpeg(), "-y", "-ss", f"{t:.2f}", "-i", str(video),
                "-frames:v", "1", "-q:v", "2", str(f),
            ], timeout=120)
            if f.exists() and not ffmpeg.is_black_frame(f):
                frames.append(f)
            elif f.exists():
                log.debug("[%s] 候选帧 t=%.1fs 是黑帧，跳过", sc, t)
                f.unlink(missing_ok=True)
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


def _pick_brightest(frames: list[Path]) -> tuple[Path, float] | None:
    """候选里挑最亮的一帧（vision 不可用时的回退，比固定第 2 帧稳）。"""
    best: tuple[Path, float] | None = None
    for f in frames:
        y = ffmpeg.mean_luma(f)
        if y is None:
            continue
        if best is None or y > best[1]:
            best = (f, y)
    return best


def _cover_is_dark(cover: Path) -> bool:
    y = ffmpeg.mean_luma(cover)
    return y is not None and y < DARK_COVER_LUMA


def _extract_representative_frame(video: Path, out_path: Path) -> bool:
    """全片暗色素材的兜底：thumbnail 滤镜选信息量代表帧。成功返回 True。"""
    try:
        ffmpeg._run([  # noqa: SLF001 —— 同模块风格，复用 ffmpeg 调用
            ffmpeg._ffmpeg(), "-y", "-i", str(video),
            "-vf", "thumbnail=200", "-frames:v", "1", "-q:v", "2",
            str(out_path),
        ], timeout=120)
        return out_path.exists() and out_path.stat().st_size > 2500
    except Exception as e:
        log.debug("代表帧抽取失败：%s", str(e)[:80])
        return False


def pick_best_cover(video: Path, work_dir: Path, sc: str,
                    out_path: Path, s) -> Path:
    """主入口：选优封面写到 out_path。

    降级链：候选帧抽取失败 → 第 1 秒截帧（旧行为）；
    vision 打分失败 → 候选帧里取最亮帧（2026-10-25 前是固定第 2 帧）。
    选定后过暗校验：luma < DARK_COVER_LUMA 的帧不当封面，全暗时用
    thumbnail 代表帧兜底（暗色素材也该有能看清的封面）。
    永不抛异常 —— 封面选优失败不能挡住发布。
    """
    try:
        frames = _extract_candidates(video, work_dir, sc)
        if not frames:
            ffmpeg.extract_cover(video, out_path, at=1.0)
            _maybe_pad_cover(video, out_path)
            return out_path

        ranked = _vision_rank(frames, s)
        chosen: Path | None = None
        note = ""
        if ranked:
            # 按分降序取第一个「不暗」的帧；分数最高的太暗就顺延
            for cand, score, _has_person in ranked:
                if not _cover_is_dark(cand):
                    chosen = cand
                    note = f"{score:.1f} 分"
                    break
            if chosen is None:
                log.warning("[%s] vision 高分帧全部过暗，改用代表帧兜底", sc)
        else:
            bright = _pick_brightest(frames)
            if bright and not _cover_is_dark(bright[0]):
                chosen = bright[0]
                note = f"最亮帧 luma={bright[1]:.0f}"

        if chosen is not None:
            out_path.write_bytes(chosen.read_bytes())
            log.info("[%s] 封面选定：%s（%s，共 %d 候选）",
                     sc, chosen.name, note, len(frames))
        elif _extract_representative_frame(video, out_path):
            log.info("[%s] 封面使用全片代表帧（候选均过暗）", sc)
        else:
            # 代表帧也失败：退回最亮候选，哪怕暗（总比没有强）
            bright = _pick_brightest(frames)
            fallback = bright[0] if bright else frames[min(1, len(frames) - 1)]
            out_path.write_bytes(fallback.read_bytes())
            log.info("[%s] 封面兜底：最亮候选 %s", sc, fallback.name)
        _maybe_pad_cover(video, out_path)
        return out_path
    except Exception as e:
        log.warning("[%s] 封面选优异常（退回第 1 秒截帧）：%s", sc, str(e)[:120])
        try:
            ffmpeg.extract_cover(video, out_path, at=1.0)
            _maybe_pad_cover(video, out_path)
        except Exception:
            pass
        return out_path


def _maybe_pad_cover(video: Path, cover: Path) -> None:
    """横屏视频 / 横图封面铺成 9:16，避免平台预览上下黑边。"""
    if not cover.exists():
        return
    try:
        w = h = 0
        if video.exists():
            info = ffmpeg.video_info(video)
            w = info.get("width") or 0
            h = info.get("height") or 0
        cw = ch = 0
        try:
            import json as _json
            proc = ffmpeg._run([  # noqa: SLF001
                ffmpeg._ffprobe(), "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", str(cover),
            ], timeout=20)
            st = (_json.loads(proc.stdout).get("streams") or [{}])[0]
            cw = int(st.get("width") or 0)
            ch = int(st.get("height") or 0)
        except Exception:
            pass
        if ffmpeg.is_landscape(w, h) or ffmpeg.is_landscape(cw, ch):
            ffmpeg.pad_image_to_vertical(cover, cover)
    except Exception:
        pass

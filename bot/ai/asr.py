"""语音识别：API（OpenAI 兼容）/ 本地 faster-whisper 双通道，输出带时间戳片段。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..config import Settings
from ..media.ffmpeg import video_info
from ..media.subtitles import Segment

log = logging.getLogger(__name__)


class ASRError(RuntimeError):
    pass


# 无人声/纯 BGM 时 Whisper（尤其 language=zh）常幻听出片尾水印
_NOISE_RE = re.compile(
    r"(字幕\s*by|字幕制作|索兰娅|请不吝点赞|订阅频道|谢谢观看|"
    r"点个关注|关注我吧|制作字幕)",
    re.I,
)


def _clean_segments(segs: list[Segment]) -> list[Segment]:
    kept = []
    for seg in segs:
        text = (seg.text or "").strip()
        if not text or _NOISE_RE.search(text):
            continue
        kept.append(seg)
    if not kept:
        return []
    joined = "".join(x.text for x in kept).strip()
    if len(joined) < 4 or _NOISE_RE.fullmatch(joined):
        return []
    return kept


def transcribe(video: Path, s: Settings) -> list[Segment]:
    """识别视频语音。无音轨/无人声/纯 BGM 时返回空列表（上层据此跳过字幕环节）。"""
    # faster-whisper 对无音轨文件会在取 streams.audio[0] 时抛 IndexError，先挡掉
    if not video_info(video)["has_audio"]:
        log.info("视频无音轨，跳过语音识别（不生成字幕）")
        return []
    if s.asr_provider == "local":
        segs = _local(video, s)
    else:
        segs = _api(video, s)
    segs = [x for x in segs if x.text]
    before = len(segs)
    segs = _clean_segments(segs)
    if before and not segs:
        log.info("识别结果均为水印/幻听，按无人声处理")
    log.info("语音识别完成：%d 段（provider=%s）", len(segs), s.asr_provider)
    return segs


def _api(video: Path, s: Settings) -> list[Segment]:
    try:
        import httpx
        from openai import OpenAI
    except ImportError as e:
        raise ASRError(f"依赖缺失：{e}") from e
    if not s.ai_api_key:
        raise ASRError("缺少 AI_API_KEY，无法调用语音识别接口；也可在 .env 设 ASR_PROVIDER=local 走本地")
    client = OpenAI(base_url=s.ai_base_url, api_key=s.ai_api_key,
                    timeout=httpx.Timeout(600, connect=15))
    with video.open("rb") as f:
        try:
            resp = client.audio.transcriptions.create(
                model=s.asr_model,
                file=f,
                language=s.asr_language or None,
                response_format="verbose_json",
            )
        except Exception as e:
            raise ASRError(
                f"API 语音识别失败：{e}\n"
                "若你的中转站不支持 /audio/transcriptions，可在 .env 改 ASR_PROVIDER=local 用本地识别"
            ) from e
    raw = getattr(resp, "segments", None) or []
    if raw:
        return [Segment(float(x.start), float(x.end), (x.text or "").strip()) for x in raw]
    # 部分兼容端只返回纯文本：整段一条字幕
    text = (getattr(resp, "text", "") or "").strip()
    if text:
        duration = video_info(video)["duration"] or 10.0
        return [Segment(0.0, duration, text)]
    return []


_local_model = None
_local_model_key: tuple | None = None


def _local(video: Path, s: Settings) -> list[Segment]:
    global _local_model, _local_model_key
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise ASRError(
            "未安装 faster-whisper。安装：pip install -r requirements-local-asr.txt，"
            "或改用 ASR_PROVIDER=api"
        ) from e
    key = (s.asr_local_model, s.asr_local_device, s.asr_local_compute)
    if _local_model is None or _local_model_key != key:
        log.info("加载本地模型 %s（首次运行会自动下载）...", s.asr_local_model)
        _local_model = WhisperModel(
            s.asr_local_model, device=s.asr_local_device, compute_type=s.asr_local_compute
        )
        _local_model_key = key
    kwargs = {"vad_filter": True}
    if s.asr_language:
        kwargs["language"] = s.asr_language
    segments, _info = _local_model.transcribe(str(video), **kwargs)
    return [Segment(seg.start, seg.end, seg.text.strip()) for seg in segments]

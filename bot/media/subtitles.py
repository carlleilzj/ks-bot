"""字幕：ASR 片段 -> SRT -> ASS（烧录用，样式随视频尺寸自适应）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import SubtitleConfig

log = logging.getLogger(__name__)

# macOS 系统中文字体：family -> 字体文件。新版 macOS 已不带 PingFang.ttc，
# 需自动回退到实际存在的字体，否则 libass 渲染出豆腐块。
_MAC_FONT_FILES = {
    "PingFang SC": "/System/Library/Fonts/PingFang.ttc",
    "Hiragino Sans GB": "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "Heiti SC": "/System/Library/Fonts/STHeiti Medium.ttc",
}


def resolve_font(preferred: str) -> str:
    """返回本机实际可用的字体 family（macOS 下按字体文件探测，其他平台原样返回）。"""
    if preferred in _MAC_FONT_FILES and Path(_MAC_FONT_FILES[preferred]).exists():
        return preferred
    for family, path in _MAC_FONT_FILES.items():
        if Path(path).exists():
            log.info("字幕字体 %s 本机不可用，回退为 %s", preferred, family)
            return family
    # 非 macOS 或无系统字体：交给 fontconfig 按名称解析
    return preferred


@dataclass
class Segment:
    start: float
    end: float
    text: str


# ---------- SRT ----------

def _fmt_srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: list[Segment]) -> str:
    blocks = []
    for i, seg in enumerate(segments, 1):
        end = max(seg.end, seg.start + 0.5)  # 防止 0 秒幕
        blocks.append(f"{i}\n{_fmt_srt_time(seg.start)} --> {_fmt_srt_time(end)}\n{seg.text}\n")
    return "\n".join(blocks)


def write_srt(path: Path, segments: list[Segment]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(segments_to_srt(segments), encoding="utf-8")
    return path


# ---------- ASS ----------

_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H7F000000,{bold},0,0,0,100,100,0,0,1,{outline},0,2,10,10,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _fmt_ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 3600_00)
    m, cs = divmod(cs, 60_00)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _sanitize(text: str) -> str:
    # ASS 特殊字符转义：{} 是标签语法，换行用 \N
    return text.replace("{", "(").replace("}", ")").replace("\n", "\\N").strip()


def segments_to_ass(segments: list[Segment], width: int, height: int, cfg: SubtitleConfig,
                    font: str | None = None) -> str:
    font = font or cfg.font
    font_size = max(24, int(width * cfg.font_size_ratio))
    outline = max(2, int(width * cfg.outline_ratio))
    margin_v = max(20, int(height * cfg.margin_v_ratio))
    header = _ASS_HEADER.format(
        width=width or 1080, height=height or 1920,
        font=font, font_size=font_size, outline=outline,
        margin_v=margin_v, bold=-1 if cfg.bold else 0,
    )
    lines = []
    for seg in segments:
        end = max(seg.end, seg.start + 0.5)
        text = _sanitize(seg.text)
        if not text:
            continue
        lines.append(
            f"Dialogue: 0,{_fmt_ass_time(seg.start)},{_fmt_ass_time(end)},Default,,0,0,0,,{text}"
        )
    return header + "\n".join(lines) + "\n"


def write_ass(path: Path, segments: list[Segment], width: int, height: int, cfg: SubtitleConfig) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    font = resolve_font(cfg.font)
    path.write_text(segments_to_ass(segments, width, height, cfg, font=font), encoding="utf-8")
    return path

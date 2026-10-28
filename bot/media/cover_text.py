"""封面大字报：在发布封面（1280×960 版）中央叠标题钩子大字。

数据背景（2026-10-29，A 号 89 条带播放统计样本）：快手主流量在双列发现页，
用户先看封面再决定点不点，封面是点击率的第一决定因素；无文字封面对
点击的贡献只剩画面本身。行业标配是封面叠 2 行以内的大字钩子。

设计约束：
- 文字放画面中央（双列流缩略图无论怎么裁都保中心，上下/左右裁都活）
- 白字黑描边，不加大底框（少遮内容），行数 ≤2、每行 ≤10 字，超长截断
- 字体缺失 / 任何异常都静默返回原图，绝不阻断发布（与封面上传同一容错级别）
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# 依次探测的字体路径（发布端为阿里云 Debian；本机 macOS 供本地调试）
_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]

_font_cache: str | None | bool = False  # False = 未探测；None = 探测过但没找到

# 标题清理：剥遗留栏目前缀与空白
_LEGACY_PREFIXES = ("【有点视频】", "【无声治愈】", "【无声小剧场】", "【纯享放松】")

MAX_LINE_CHARS = 10
MAX_LINES = 2


def _clean_title(title: str) -> str:
    t = (title or "").strip()
    for p in _LEGACY_PREFIXES:
        t = t.replace(p, "")
    return t.strip()


def wrap_title(title: str, max_chars: int = MAX_LINE_CHARS,
               max_lines: int = MAX_LINES) -> list[str]:
    """把标题切成大字报行：≤max_chars 字/行、≤max_lines 行，超长末行加省略号。"""
    t = _clean_title(title)
    if not t:
        return []
    chunks: list[str] = []
    for i in range(0, len(t), max_chars):
        chunks.append(t[i:i + max_chars])
    if len(chunks) > max_lines:
        chunks = chunks[:max_lines]
        last = chunks[-1]
        if len(last) >= max_chars:
            last = last[:max_chars - 1]
        chunks[-1] = last.rstrip("，。！？、, ") + "…"
    return chunks


def _font_path() -> str | None:
    """找一个可用的 CJK 字体；结果缓存（每次发布进程内只探测一次）。"""
    global _font_cache
    if _font_cache is not False:
        return _font_cache or None  # type: ignore[return-value]
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            _font_cache = p
            return p
    try:
        out = subprocess.run(
            ["fc-match", "-f", "%{file}", "Noto Sans CJK SC"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if out and Path(out).exists():
            _font_cache = out
            return out
    except Exception:
        pass
    _font_cache = None
    return None


def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    """加载字体；ttc 多面优先挑名字含 SC 的面（简体字形正确）。"""
    for idx in range(8):
        try:
            f = ImageFont.truetype(path, size, index=idx)
            name = " ".join(f.getname())
            if "SC" in name:
                return f
        except Exception:
            break
    return ImageFont.truetype(path, size)


def overlay_title_text(img: Path, title: str) -> Path:
    """在封面图中央叠标题大字，返回新文件（<stem>_titled.jpg）。

    任何失败（无字体/图片打不开/画字异常）都返回原图路径，绝不抛。
    """
    lines = wrap_title(title)
    if not lines:
        return img
    font_path = _font_path()
    if not font_path:
        log.info("封面大字跳过：未找到 CJK 字体")
        return img
    out_path = img.with_name(img.stem + "_titled.jpg")
    try:
        with Image.open(img) as im:
            im = im.convert("RGB")
            w, h = im.size
            longest = max(len(l) for l in lines)
            # 最长行占画面宽 ~92%，并夹在 [w/14, w/7] 区间防极端
            size = int(w * 0.92 / max(longest, 1))
            size = max(w // 14, min(w // 7, size))
            font = _load_font(font_path, size)
            stroke = max(2, size // 16)
            line_h = int(size * 1.22)
            total_h = line_h * len(lines)
            y0 = max(int(h * 0.5 - total_h * 0.55), int(h * 0.08))  # 略偏上居中
            draw = ImageDraw.Draw(im)
            for i, line in enumerate(lines):
                bbox = draw.textbbox((0, 0), line, font=font,
                                     stroke_width=stroke)
                lw = bbox[2] - bbox[0]
                x = (w - lw) // 2
                y = y0 + i * line_h
                draw.text((x, y), line, font=font, fill=(255, 255, 255),
                          stroke_width=stroke, stroke_fill=(0, 0, 0))
            im.save(out_path, "JPEG", quality=92)
        log.info("封面大字已叠：%s（%d 行）", out_path.name, len(lines))
        return out_path
    except Exception as e:
        log.warning("封面大字失败（按原图发布）：%s", str(e)[:120])
        return img

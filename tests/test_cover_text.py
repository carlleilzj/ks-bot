"""封面大字报（bot/media/cover_text.py）单测：断行、截断、兜底不抛。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from bot.media import cover_text

# ---------- wrap_title ----------

def test_wrap_title_short_single_line():
    assert cover_text.wrap_title("安静看完，治愈又解压") == ["安静看完，治愈又解压"]


def test_wrap_title_splits_two_lines():
    lines = cover_text.wrap_title("要是卧室能铺满这种软垫，爬过去睡觉我都认了")
    assert len(lines) == 2
    assert all(len(l) <= cover_text.MAX_LINE_CHARS for l in lines)
    assert "".join(lines).startswith("要是卧室能铺满这种软垫")


def test_wrap_title_truncates_with_ellipsis():
    t = "要" * 25
    lines = cover_text.wrap_title(t)
    assert len(lines) == cover_text.MAX_LINES
    assert lines[-1].endswith("…")
    assert len(lines[-1]) <= cover_text.MAX_LINE_CHARS


def test_wrap_title_strips_legacy_prefix_and_empty():
    assert cover_text.wrap_title("【纯享放松】安静看完") == ["安静看完"]
    assert cover_text.wrap_title("【有点视频】") == []
    assert cover_text.wrap_title("") == []
    assert cover_text.wrap_title(None) == []


# ---------- 字体探测 ----------

def test_font_path_found_or_none():
    p = cover_text._font_path()
    assert p is None or Path(p).exists()


# ---------- overlay_title_text ----------

def _solid_cover(tmp_path: Path, size=(1280, 960)) -> Path:
    img = tmp_path / "cover.jpg"
    Image.new("RGB", size, (90, 140, 200)).save(img, "JPEG")
    return img


def test_overlay_creates_titled_image(tmp_path):
    if not cover_text._font_path():
        pytest.skip("本机无 CJK 字体，无法验证叠字")
    img = _solid_cover(tmp_path)
    out = cover_text.overlay_title_text(img, "要是家里有这么一只，下班都想早点回去")
    assert out != img and out.exists()
    with Image.open(out) as im:
        assert im.size == (1280, 960)
        # 中央区域被白字+黑描边改过：与纯色原图的方差应显著大于角落
        px = im.load()

        def diff(a, b) -> int:
            return sum(abs(x - y) for x, y in zip(a, b))

        center = [px[640, dy] for dy in range(380, 580, 20)]
        corner = [px[20, dy] for dy in range(20, 220, 20)]
        assert max(diff(c, c2) for c, c2 in zip(center, center[1:])) > 0
        assert all(diff(c, (90, 140, 200)) < 30 for c in corner)


def test_overlay_no_font_returns_original(tmp_path, monkeypatch):
    monkeypatch.setattr(cover_text, "_font_cache", None)  # 已探测、无字体
    img = _solid_cover(tmp_path, size=(640, 480))
    assert cover_text.overlay_title_text(img, "标题") == img


def test_overlay_bad_image_returns_original(tmp_path, monkeypatch):
    # 强制有字体（若真没有则跳过），但喂一个损坏文件：必须按原图返回、不抛
    if not cover_text._font_path():
        pytest.skip("本机无 CJK 字体")
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not-a-jpeg")
    assert cover_text.overlay_title_text(bad, "标题") == bad


def test_overlay_empty_title_returns_original(tmp_path):
    img = _solid_cover(tmp_path)
    assert cover_text.overlay_title_text(img, "") == img
    assert cover_text.overlay_title_text(img, "【有点视频】") == img

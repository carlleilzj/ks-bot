"""水印擦除（delogo）与配置解析测试。"""

from __future__ import annotations

import pytest

from bot.config import PRESET_REGIONS, WatermarkConfig, _build_watermark
from bot.media.ffmpeg import _to_pixels


# ---------- 配置解析 ----------

def test_watermark_disabled_by_default():
    cfg = _build_watermark(None)
    assert cfg.enabled is False
    assert cfg.regions == []


def test_watermark_presets_expand():
    cfg = _build_watermark({"enabled": True, "presets": ["corner_pair"]})
    assert cfg.regions == PRESET_REGIONS["corner_pair"]
    assert len(cfg.regions) == 2


def test_watermark_multiple_presets_merge():
    cfg = _build_watermark({"enabled": True, "presets": ["top_left", "bottom_right"]})
    assert len(cfg.regions) == 2


def test_watermark_custom_regions():
    cfg = _build_watermark({"enabled": True,
                            "regions": [[100, 50, 200, 80], [0.1, 0.1, 0.2, 0.2]]})
    assert cfg.regions == [[100.0, 50.0, 200.0, 80.0], [0.1, 0.1, 0.2, 0.2]]


def test_watermark_bad_region_skipped():
    cfg = _build_watermark({"enabled": True,
                            "regions": [[1, 2, 3], "bad", [0.1, 0.1, 0.2, 0.2]]})
    assert cfg.regions == [[0.1, 0.1, 0.2, 0.2]]


def test_watermark_unknown_preset_ignored():
    cfg = _build_watermark({"enabled": True, "presets": ["nonexistent"]})
    assert cfg.regions == []


def test_watermark_box_parsed():
    cfg = _build_watermark({"enabled": True, "box": 3})
    assert cfg.box == 3


# ---------- 坐标换算 ----------

def test_to_pixels_relative():
    """全 <=1 视为相对比例。"""
    px, py, pw, ph = _to_pixels((0.02, 0.02, 0.16, 0.10), 1000, 500)
    assert (px, py, pw, ph) == (20, 10, 160, 50)


def test_to_pixels_absolute():
    """存在 >1 的值 → 视为绝对像素。"""
    px, py, pw, ph = _to_pixels((100, 50, 200, 80), 1000, 500)
    assert (px, py, pw, ph) == (100, 50, 200, 80)


def test_to_pixels_clamped_inside_frame():
    """越界坐标被钳制在画面内，避免 delogo 报错。"""
    px, py, pw, ph = _to_pixels((990, 490, 500, 500), 1000, 500)
    assert px + pw <= 999
    assert py + ph <= 499
    assert pw >= 2 and ph >= 2


def test_to_pixels_min_size():
    """极小区域被抬到最小 2x2（delogo 要求）。"""
    px, py, pw, ph = _to_pixels((0.0, 0.0, 0.0001, 0.0001), 1000, 500)
    assert pw >= 2 and ph >= 2


def test_preset_corner_pair_within_bounds():
    """预置 corner_pair 的区域在常见竖屏下不越界。"""
    for region in PRESET_REGIONS["corner_pair"]:
        px, py, pw, ph = _to_pixels(tuple(region), 720, 1280)
        assert 0 <= px < 720
        assert 0 <= py < 1280
        assert px + pw <= 720
        assert py + ph <= 1280

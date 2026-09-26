"""横屏铺竖屏 + 黑帧判定（YouTube 黑封面回归）。"""

from __future__ import annotations

from bot.media.ffmpeg import (
    COVER_LANDSCAPE,
    COVER_PORTRAIT,
    TRANSFORM_DEFAULTS,
    cover_scale_crop_filter,
    is_black_frame,
    is_landscape,
    transform_filter,
)


def test_landscape_16_9():
    assert is_landscape(1280, 720) is True
    assert is_landscape(640, 360) is True
    assert is_landscape(1920, 1080) is True


def test_portrait_9_16_not_padded():
    assert is_landscape(720, 1280) is False
    assert is_landscape(1080, 1920) is False


def test_square_is_padded():
    assert is_landscape(720, 720) is True


def test_zero_size_not_landscape():
    assert is_landscape(0, 720) is False
    assert is_landscape(720, 0) is False


def test_black_frame_missing_file(tmp_path):
    assert is_black_frame(tmp_path / "nope.jpg") is True


def test_black_frame_tiny_file(tmp_path):
    p = tmp_path / "tiny.jpg"
    p.write_bytes(b"x" * 100)
    assert is_black_frame(p) is True


def test_cover_crop_sizes():
    assert COVER_PORTRAIT == (960, 1280)
    assert COVER_LANDSCAPE == (1280, 960)


def test_cover_scale_crop_filter_3_4():
    f = cover_scale_crop_filter(960, 1280)
    assert "960:1280" in f
    assert "crop=960:1280" in f


def test_cover_scale_crop_filter_4_3():
    f = cover_scale_crop_filter(1280, 960)
    assert "1280:960" in f
    assert "force_original_aspect_ratio=increase" in f


# ---------------------------------------------------------------------------
# 二创变换（抖音「原创性不足」）
# ---------------------------------------------------------------------------

_INFO = {"width": 720, "height": 1280}


def test_transform_filter_keeps_output_size():
    """裁切/放大后必须 scale 回原尺寸，否则竖屏约束与封面比例全乱。"""
    import random
    vf, params = transform_filter(random.Random(1), _INFO)
    assert vf
    assert f"scale=720:1280" in vf
    assert "crop=" in vf
    assert params["zoom"] > 1.0


def test_transform_filter_even_crop_dims():
    """yuv420p 要求裁切宽高为偶数，否则 ffmpeg 直接报错。"""
    import random
    for seed in range(12):
        vf, _ = transform_filter(random.Random(seed), _INFO)
        crop = [p for p in vf.split(",") if p.startswith("crop=")][0]
        w, h, x, y = (int(v) for v in crop.split("=")[1].split(":"))
        assert w % 2 == 0 and h % 2 == 0, (seed, crop)
        assert x % 2 == 0 and y % 2 == 0, (seed, crop)


def test_transform_filter_randomized_per_seed():
    """两条片子参数必须不同，固定模板反而会被判同质化。"""
    import random
    a, pa = transform_filter(random.Random(11), _INFO)
    b, pb = transform_filter(random.Random(99), _INFO)
    assert a != b or pa != pb


def test_transform_filter_unknown_size_returns_empty():
    assert transform_filter(__import__("random").Random(1), {"width": 0, "height": 0}) == ("", {})


def test_transform_filter_color_always_present():
    import random
    vf, _ = transform_filter(random.Random(5), _INFO)
    assert "eq=" in vf


def test_transform_defaults_has_all_keys():
    for k in ("crop_pct", "zoom_pct", "hflip_prob", "hue_deg", "sat",
              "bright", "contrast", "fps", "tempo", "crf"):
        assert k in TRANSFORM_DEFAULTS

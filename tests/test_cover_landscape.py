"""横屏铺竖屏 + 黑帧判定（YouTube 黑封面回归）。"""

from __future__ import annotations

from bot.media.ffmpeg import is_black_frame, is_landscape


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

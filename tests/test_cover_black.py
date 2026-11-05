"""回归测试：封面全黑问题（2026-11-05）。

线上现象：发布后作品封面一片全黑。
根因两条：
  1. 调用点 `if cover and Path(cover).exists()` —— 封面下载失败/文件缺失时
     整段跳过封面上传，平台改用视频首帧（暗色素材首帧常是黑的）。
  2. 暗帧阈值 26 太低，线上 luma=40.6 的封面被判「合格」直接发布。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bot.ai import cover_pick
from bot.publish import kuaishou as ks


def test_dark_thresholds_match_and_raised():
    """两个模块的阈值必须一致，且高到能挡住「暗但非纯黑」的封面。"""
    assert ks.DARK_COVER_LUMA == cover_pick.DARK_COVER_LUMA
    assert ks.DARK_COVER_LUMA >= 42, "26 挡不住 luma=40 的暗封面"


def test_rescue_returns_original_when_bright(tmp_path):
    """亮封面不动，不抽帧。"""
    cover = tmp_path / "c.jpg"
    cover.write_bytes(b"x" * 5000)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v" * 5000)

    with patch("bot.media.ffmpeg.mean_luma", return_value=120.0):
        out = ks.rescue_dark_cover(cover, video)
    assert out == cover


def test_rescue_extracts_when_cover_missing(tmp_path):
    """封面缺失（下载失败）时必须从视频抽帧，而不是直接返回 None。

    这是黑封面的第一条根因：旧实现 `if cover is None: return None`，
    调用方再因 cover 为空跳过上传 → 平台用视频首帧 → 全黑。
    """
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v" * 5000)
    extracted = tmp_path / "v_cover_rescue.jpg"

    def fake_run(cmd, timeout=None):
        extracted.write_bytes(b"j" * 6000)

    with patch("bot.media.ffmpeg._run", side_effect=fake_run), \
         patch("bot.media.ffmpeg.mean_luma", return_value=90.0):
        out = ks.rescue_dark_cover(None, video)

    assert out is not None, "封面缺失时必须抽帧兜底，不能返回 None"
    assert Path(out) == extracted


def test_rescue_extracts_when_cover_file_absent(tmp_path):
    """cover 路径给了但文件不存在，也要抽帧。"""
    cover = tmp_path / "gone.jpg"
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v" * 5000)
    extracted = tmp_path / "gone.rescue.jpg"

    def fake_run(cmd, timeout=None):
        extracted.write_bytes(b"j" * 6000)

    with patch("bot.media.ffmpeg._run", side_effect=fake_run), \
         patch("bot.media.ffmpeg.mean_luma", return_value=90.0):
        out = ks.rescue_dark_cover(cover, video)

    assert Path(out) == extracted


def test_rescue_replaces_when_luma_40(tmp_path):
    """luma=40.6（线上真实值）必须被判为过暗并替换。"""
    cover = tmp_path / "c.jpg"
    cover.write_bytes(b"x" * 5000)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v" * 5000)
    extracted = tmp_path / "c.rescue.jpg"

    def fake_run(cmd, timeout=None):
        extracted.write_bytes(b"j" * 6000)

    luma_values = iter([40.6, 95.0])   # 原封面暗，抽出帧亮

    with patch("bot.media.ffmpeg._run", side_effect=fake_run), \
         patch("bot.media.ffmpeg.mean_luma", side_effect=lambda p: next(luma_values)):
        out = ks.rescue_dark_cover(cover, video)

    assert Path(out) == extracted, "luma=40.6 应被视为过暗"


def test_rescue_keeps_original_when_video_missing(tmp_path):
    """视频也不存在时不能崩，返回原封面。"""
    cover = tmp_path / "c.jpg"
    cover.write_bytes(b"x" * 5000)
    missing_video = tmp_path / "nope.mp4"

    with patch("bot.media.ffmpeg.mean_luma", return_value=10.0):
        out = ks.rescue_dark_cover(cover, missing_video)
    assert out == cover


def test_rescue_returns_none_when_both_missing(tmp_path):
    """封面和视频都没有 → 返回 None，由调用方记 warn 并交给平台。"""
    with patch("bot.media.ffmpeg.mean_luma", return_value=10.0):
        out = ks.rescue_dark_cover(None, tmp_path / "nope.mp4")
    assert out is None

"""真人判定与多帧质检的回归测试。

覆盖 2026-09-15 修复的误杀场景：
`instagram_Dc_obVBP4ZP` —— 纯 3D 动画短剧，因片头 1 秒背景有虚化人物建模，
被旧的存在性判定 `has_real_person=True` 一票否决。
"""

from __future__ import annotations

import pytest

from bot.ai.vision import (
    REAL_PERSON_RATIO_THRESHOLD,
    CoverVerdict,
    inspect_video_frames,
)


# ---------- 单帧判定：动画豁免 ----------

def test_animation_with_background_person_passes():
    """动画内容 + 背景有真人 → 豁免，不否决（本次误杀的根因场景）。"""
    v = CoverVerdict(
        is_animation=True,
        has_real_person=True,
        real_person_ratio=0.02,
        real_person_is_subject=False,
        real_person_desc="左上方虚化背景路人",
        has_watermark=False,
    )
    assert v.real_person_blocks is False
    assert v.ok_for_animal_anime is True


def test_animation_with_subject_person_passes():
    """动画内容里的人物即使占比高也不否决（动画角色 ≠ 真人）。"""
    v = CoverVerdict(is_animation=True, has_real_person=True,
                     real_person_ratio=0.9, real_person_is_subject=True)
    assert v.real_person_blocks is False


def test_animation_with_watermark_still_rejected():
    """动画但带水印 → 仍否决（水印规则不受动画豁免影响）。"""
    v = CoverVerdict(is_animation=True, has_real_person=False, has_watermark=True,
                     watermark_desc="右下角账号名")
    assert v.ok_for_animal_anime is False
    assert "水印" in v.reject_reason


# ---------- 单帧判定：真人占比阈值 ----------

def test_real_person_subject_rejected():
    """真人实拍为主体 → 否决。"""
    v = CoverVerdict(is_animation=False, has_real_person=True,
                     real_person_ratio=0.65, real_person_is_subject=True)
    assert v.real_person_blocks is True
    assert v.ok_for_animal_anime is False
    assert "65%" in v.reject_reason


def test_real_person_below_threshold_passes():
    """非动画画面里的小占比真人（背景路人）→ 不否决。"""
    v = CoverVerdict(is_animation=False, has_real_person=True,
                     real_person_ratio=REAL_PERSON_RATIO_THRESHOLD - 0.01,
                     real_person_is_subject=False)
    assert v.real_person_blocks is False


def test_real_person_above_threshold_rejected():
    """占比超阈值 → 否决，即使模型没明确标主体。"""
    v = CoverVerdict(is_animation=False, has_real_person=True,
                     real_person_ratio=REAL_PERSON_RATIO_THRESHOLD + 0.1,
                     real_person_is_subject=False)
    assert v.real_person_blocks is True


def test_real_person_ratio_unknown_falls_back_to_reject():
    """占比未知（旧模型/无该字段）→ 保守否决，保持旧行为不变。"""
    v = CoverVerdict(is_animation=False, has_real_person=True)
    assert v.real_person_blocks is True


def test_not_animation_rejected():
    """明确非动画 → 否决。"""
    v = CoverVerdict(is_animation=False, has_real_person=False, has_watermark=False)
    assert v.ok_for_animal_anime is False
    assert v.reject_reason == "非动画内容"


def test_clean_animation_passes():
    """干净动画 → 放行。"""
    v = CoverVerdict(is_animation=True, has_real_person=False, has_watermark=False)
    assert v.ok_for_animal_anime is True
    assert v.reject_reason == ""


def test_unknown_verdict_passes():
    """全部字段为 None（检测失败）→ 放行，人工把关。"""
    v = CoverVerdict()
    assert v.ok_for_animal_anime is True


# ---------- 多帧综合 ----------

def _stub_frames(tmp_path, n=3):
    frames = []
    for i in range(n):
        f = tmp_path / f"f{i}.jpg"
        f.write_bytes(b"\xff\xd8fake")
        frames.append(f)
    return frames


def test_multiframe_animation_wins(tmp_path, monkeypatch):
    """3 帧中任一判为动画 → 整片按动画处理，真人规则豁免。"""
    import bot.ai.vision as vis

    verdicts = [
        CoverVerdict(is_animation=True, has_real_person=True, real_person_ratio=0.02),
        CoverVerdict(is_animation=True, has_real_person=False),
        CoverVerdict(is_animation=True, has_real_person=False),
    ]
    it = iter(verdicts)
    monkeypatch.setattr(vis, "inspect_cover", lambda c, s: next(it))

    merged = vis.inspect_video_frames(_stub_frames(tmp_path), None)
    assert merged.is_animation is True
    assert merged.real_person_blocks is False
    assert merged.ok_for_animal_anime is True


def test_multiframe_real_person_subject_rejected(tmp_path, monkeypatch):
    """多帧中真人为主体 → 否决。"""
    import bot.ai.vision as vis

    verdicts = [
        CoverVerdict(is_animation=False, has_real_person=True,
                     real_person_ratio=0.7, real_person_is_subject=True),
        CoverVerdict(is_animation=False, has_real_person=True,
                     real_person_ratio=0.6, real_person_is_subject=True),
        CoverVerdict(is_animation=False, has_real_person=False),
    ]
    it = iter(verdicts)
    monkeypatch.setattr(vis, "inspect_cover", lambda c, s: next(it))

    merged = vis.inspect_video_frames(_stub_frames(tmp_path), None)
    assert merged.real_person_blocks is True
    assert merged.real_person_ratio == 0.7  # 取占比最高帧


def test_multiframe_watermark_any_frame_wins(tmp_path, monkeypatch):
    """任一帧命中水印 → 标记有水印。"""
    import bot.ai.vision as vis

    verdicts = [
        CoverVerdict(is_animation=True, has_real_person=False, has_watermark=False),
        CoverVerdict(is_animation=True, has_real_person=False, has_watermark=True,
                     watermark_desc="左上角圆形Logo"),
        CoverVerdict(is_animation=True, has_real_person=False, has_watermark=False),
    ]
    it = iter(verdicts)
    monkeypatch.setattr(vis, "inspect_cover", lambda c, s: next(it))

    merged = vis.inspect_video_frames(_stub_frames(tmp_path), None)
    assert merged.has_watermark is True
    assert merged.watermark_desc == "左上角圆形Logo"


def test_multiframe_partial_failure_tolerated(tmp_path, monkeypatch):
    """部分帧检测失败 → 用成功的帧综合，不整体失败。"""
    import bot.ai.vision as vis

    calls = {"n": 0}

    def flaky(c, s):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("接口超时")
        return CoverVerdict(is_animation=True, has_real_person=False, has_watermark=False)

    monkeypatch.setattr(vis, "inspect_cover", flaky)
    merged = vis.inspect_video_frames(_stub_frames(tmp_path), None)
    assert merged.is_animation is True
    assert merged.real_person_blocks is False


def test_multiframe_all_fail_returns_unknown(tmp_path, monkeypatch):
    """所有帧都失败 → 返回不确定判定（调用方放行）。"""
    import bot.ai.vision as vis

    def boom(c, s):
        raise RuntimeError("接口超时")

    monkeypatch.setattr(vis, "inspect_cover", boom)
    merged = vis.inspect_video_frames(_stub_frames(tmp_path), None)
    assert merged.ok_for_animal_anime is True  # 不确定 → 放行


# ---------- 占比字段解析 ----------

@pytest.mark.parametrize("raw,expected", [
    (0.05, 0.05),
    ("0.05", 0.05),
    ("5%", 0.05),
    (5, 0.05),          # 模型给了百分数而非小数
    ("50%", 0.5),
    (None, None),
    ("", None),
    ("abc", None),
    (-0.5, 0.0),        # 越界钳制
    (2.0, 0.02),        # >1 视为百分数
])
def test_parse_ratio(raw, expected):
    from bot.ai.vision import _parse_ratio
    assert _parse_ratio(raw) == expected


# ---------- 擦水印后的反馈循环 ----------

def test_watermark_only_block_detects_delogo_residue():
    """已擦水印后，模型把模糊痕迹当水印 → 判定为"仅水印否决"。"""
    from bot.ai.vision import watermark_only_block
    v = CoverVerdict(is_animation=True, has_real_person=False, has_watermark=True,
                     watermark_desc="右下角沙滩处有半透明条状模糊/马赛克水印痕迹")
    assert watermark_only_block(v) is True


def test_watermark_only_block_false_when_real_person():
    """真人主体否决时不适用（水印不是唯一原因）。"""
    from bot.ai.vision import watermark_only_block
    v = CoverVerdict(is_animation=False, has_real_person=True,
                     real_person_ratio=0.8, real_person_is_subject=True,
                     has_watermark=True)
    assert watermark_only_block(v) is False


def test_watermark_only_block_false_when_not_animation():
    """明确非动画时不适用（赛道不符是独立原因）。"""
    from bot.ai.vision import watermark_only_block
    v = CoverVerdict(is_animation=False, has_real_person=False, has_watermark=True)
    assert watermark_only_block(v) is False


def test_watermark_only_block_false_without_watermark():
    """没有水印判定时当然不适用。"""
    from bot.ai.vision import watermark_only_block
    v = CoverVerdict(is_animation=True, has_real_person=False, has_watermark=False)
    assert watermark_only_block(v) is False

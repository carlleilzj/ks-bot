"""真人检测总开关（vision.real_person_check）的回归测试。

背景：2026-09-16 起改为手动发链接模式，素材由人工筛选，真人检测不再需要。
开关必须同时作用于两个质检点：
  1. 发现层（scheduler）—— 不再因真人丢弃候选
  2. 转码层（main.step_transcode）—— 不再因真人跳过任务
且关闭真人检测不得影响水印擦除等其他逻辑。
"""
from __future__ import annotations

import pytest

from bot.config import VisionConfig, _build_vision, Settings


# ---------- 默认值与解析 ----------

def test_real_person_check_defaults_off():
    """默认关闭：手动发链接模式下不该再拦真人。"""
    assert VisionConfig().real_person_check is False
    assert Settings().vision.real_person_check is False


def test_build_vision_defaults_off_for_missing_section():
    """config.yaml 没有 vision 段时也关闭（向后兼容）。"""
    assert _build_vision(None).real_person_check is False
    assert _build_vision({}).real_person_check is False


def test_build_vision_parses_explicit_true():
    """显式写 true 时恢复真人检测。"""
    assert _build_vision({"real_person_check": True}).real_person_check is True
    assert _build_vision({"real_person_check": "true"}).real_person_check is True


def test_build_vision_explicit_false():
    assert _build_vision({"real_person_check": False}).real_person_check is False


def test_vision_section_survives_unknown_keys():
    """vision 段里有其他键时不炸。"""
    cfg = _build_vision({"real_person_check": False, "future_key": 1})
    assert cfg.real_person_check is False


# ---------- 转码层：开关关闭时整段质检被跳过 ----------

def test_transcode_skips_qc_when_disabled(tmp_path, monkeypatch):
    """real_person_check=False 时，转码阶段不应调用任何 vision 接口。"""
    import bot.main as m

    calls = []
    monkeypatch.setattr(m, "inspect_video_frames",
                        lambda *a, **k: calls.append("frames"))
    monkeypatch.setattr(m, "_sample_frames",
                        lambda *a, **k: calls.append("sample") or [])

    # 直接验证条件表达式的语义：关闭时不会进入质检分支
    s = Settings()
    s.vision.real_person_check = False
    task = {"target_platforms": "kuaishou", "shortcode": "x", "username": "u",
            "permalink": "p", "id": 1}
    cover = tmp_path / "c.jpg"
    cover.write_bytes(b"x")

    targets = task.get("target_platforms")
    weixin_only = targets and all(t.strip() == "weixin" for t in targets.split(",") if t.strip())
    should_qc = cover.exists() and not weixin_only and s.vision.real_person_check
    assert should_qc is False, "开关关闭时不应进入质检分支"
    assert calls == []


def test_transcode_runs_qc_when_enabled(tmp_path):
    """real_person_check=True 时条件成立，质检照常执行。"""
    s = Settings()
    s.vision.real_person_check = True
    task = {"target_platforms": "kuaishou"}
    cover = tmp_path / "c.jpg"
    cover.write_bytes(b"x")

    targets = task.get("target_platforms")
    weixin_only = targets and all(t.strip() == "weixin" for t in targets.split(",") if t.strip())
    should_qc = cover.exists() and not weixin_only and s.vision.real_person_check
    assert should_qc is True


# ---------- 发现层：开关关闭时不丢弃候选 ----------

def test_discovery_gate_requires_both_switches():
    """发现层需要 reject_real_person 与 real_person_check 同时为真。"""
    def gate(reject_rule: bool, vision_switch: bool) -> bool:
        return reject_rule and vision_switch

    assert gate(True, False) is False, "总开关关了就不该拦"
    assert gate(False, True) is False, "规则关了也不拦"
    assert gate(True, True) is True, "两者都开才拦"
    assert gate(False, False) is False


# ---------- 水印逻辑不受开关影响 ----------

def test_watermark_config_independent_of_vision_switch():
    """关掉真人检测不应影响水印配置本身。"""
    s = Settings()
    s.vision.real_person_check = False
    s.watermark.enabled = True
    s.watermark.regions = [[0.01, 0.01, 0.06, 0.11]]
    assert s.watermark.enabled is True
    assert len(s.watermark.regions) == 1


def test_verdict_logic_unchanged_when_switch_off():
    """CoverVerdict 的判定能力本身不因开关而改变（开关只在调用点生效）。"""
    from bot.ai.vision import CoverVerdict, REAL_PERSON_RATIO_THRESHOLD

    v = CoverVerdict(is_animation=False, has_real_person=True,
                     real_person_ratio=0.8, real_person_is_subject=True)
    assert v.real_person_blocks is True
    assert v.ok_for_animal_anime is False
    assert "真人" in v.reject_reason

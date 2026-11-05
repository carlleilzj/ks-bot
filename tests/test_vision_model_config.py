"""回归测试：视觉任务独立配置模型（2026-11-05）。

背景：原先所有 AI 调用共用主模型（gemini-3.8-flash-high）。实测封面打分
这类任务用轻量模型效果一致但快 5.5 倍，因此把视觉任务拆到 VISION_MODEL。
本测试锁定回退链的优先级，防止将来改动把视觉调用又退回主模型。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from bot.ai.vision import _vision_candidates


def _settings(ai_model="main-model", vision_model=""):
    s = MagicMock()
    s.ai_model = ai_model
    s.vision_model = vision_model
    return s


def test_vision_model_takes_priority():
    """配了 VISION_MODEL 时必须是候选链第一位。"""
    chain = _vision_candidates(_settings(ai_model="main", vision_model="light"))
    assert chain[0] == "light"
    assert chain[1] == "main"


def test_falls_back_to_ai_model_when_unset():
    chain = _vision_candidates(_settings(ai_model="main", vision_model=""))
    assert chain[0] == "main"


def test_no_duplicate_entries():
    chain = _vision_candidates(_settings(ai_model="same", vision_model="same"))
    assert chain.count("same") == 1


def test_chain_includes_fallbacks():
    """候选链必须有兜底模型，主模型全挂时不至于直接放弃。"""
    chain = _vision_candidates(_settings())
    assert len(chain) >= 3


def test_cover_scoring_uses_vision_candidates():
    """封面打分必须走 _vision_rank（它内部用 _vision_candidates），
    不能自己硬编码主模型。"""
    import inspect

    from bot.ai import cover_pick

    src = inspect.getsource(cover_pick._vision_rank)
    assert "_vision_candidates" in src

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


# ---------- vision 端点与 key 必须成对（2026-11-12 全站 403 事故） ----------

def test_vision_endpoint_falls_back_to_ai_base_url():
    """未配 VISION_BASE_URL 时回退 ai_base_url（同一家中转站的旧行为）。"""
    from bot.ai.vision import vision_endpoint
    from bot.config import Settings

    s = Settings()
    s.ai_base_url = "https://cpa.example/v1"
    s.ai_api_key = "sk-ai"
    s.vision_base_url = ""
    s.vision_api_key = "sk-vision"

    base, key = vision_endpoint(s)
    assert base == "https://cpa.example/v1"
    assert key == "sk-vision"


def test_vision_endpoint_uses_dedicated_base_when_set():
    """配了 VISION_BASE_URL 就用它，不再跟 AI_BASE_URL 绑死。

    事故还原：AI_BASE_URL 换成另一家中转站后，vision 的 base 跟着漂走，
    但 VISION_API_KEY 还是旧家的 → 三处视觉调用全部 403。
    """
    from bot.ai.vision import vision_endpoint
    from bot.config import Settings

    s = Settings()
    s.ai_base_url = "https://wb.example/v1"        # 文案供应商
    s.ai_api_key = "wb-key"
    s.vision_base_url = "https://cpa.example/v1"   # 视觉供应商（另一家）
    s.vision_api_key = "sk-vision"

    base, key = vision_endpoint(s)
    assert base == "https://cpa.example/v1"
    assert key == "sk-vision", "端点与 key 必须是同一家的"


def test_vision_endpoint_strips_trailing_slash():
    from bot.ai.vision import vision_endpoint
    from bot.config import Settings

    s = Settings()
    s.vision_base_url = "https://cpa.example/v1/"
    assert vision_endpoint(s)[0] == "https://cpa.example/v1"


def test_vision_endpoint_key_falls_back_to_ai_key():
    """没配 VISION_API_KEY 时用 ai_api_key（单供应商场景）。"""
    from bot.ai.vision import vision_endpoint
    from bot.config import Settings

    s = Settings()
    s.ai_base_url = "https://x/v1"
    s.ai_api_key = "sk-ai"
    s.vision_api_key = ""
    assert vision_endpoint(s)[1] == "sk-ai"


def test_all_vision_callers_use_endpoint_helper():
    """三个视觉调用点都必须走 vision_endpoint，不能再各写 s.ai_base_url。

    这条是结构约束：漏一处就会再次出现「换了 AI_BASE_URL 而 vision 静默 403」。
    """
    import inspect

    from bot.ai import content_probe, cover_pick, vision

    # describe_video 只是外层壳，真正建 OpenAI client 的是它内部的 _probe
    cases = [
        (vision, "inspect_cover"),
        (content_probe, "_ask_vision"),
        (cover_pick, "_vision_rank"),
    ]
    for mod, fn in cases:
        src = inspect.getsource(getattr(mod, fn))
        assert "vision_endpoint(s)" in src, f"{mod.__name__}.{fn} 未使用 vision_endpoint"
        assert "s.ai_base_url" not in src, f"{mod.__name__}.{fn} 仍直接用 s.ai_base_url"


def test_vision_base_url_loaded_from_env(monkeypatch):
    from bot.config import load_settings

    monkeypatch.setenv("VISION_BASE_URL", "https://cpa.doodcc.com/v1")
    s = load_settings()
    assert s.vision_base_url == "https://cpa.doodcc.com/v1"

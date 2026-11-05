"""回归测试：封面 vision 打分择优 + 全局取消大字（2026-11-05）。

需求：
1. 取消所有封面标题大字（默认值也要改成不叠）；
2. 推荐封面下载后要过一遍我们自己的 vision 打分，而不是只比亮度。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bot.config import PlatformConfig
from bot.publish import kuaishou as ks

# ---------- 1. 全局取消标题大字 ----------

def test_cover_title_defaults_false():
    assert PlatformConfig(enabled=True).cover_title is False


def test_publish_signature_defaults_no_cover_title():
    import inspect

    sig = inspect.signature(ks.publish)
    assert sig.parameters["cover_title"].default is False


def test_config_parse_defaults_false(tmp_path, monkeypatch):
    """config.yaml 没写 cover_title 时也必须不叠字。"""
    import bot.config as cfg

    yml = tmp_path / "config.yaml"
    yml.write_text("platforms:\n  kuaishou:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_PATH", yml)
    s = cfg.load_settings()
    assert s.platforms["kuaishou"].cover_title is False


def test_config_parse_explicit_true_still_works(tmp_path, monkeypatch):
    """显式写 true 仍能开启（保留可逆性）。"""
    import bot.config as cfg

    yml = tmp_path / "config.yaml"
    yml.write_text(
        "platforms:\n  kuaishou:\n    enabled: true\n    cover_title: true\n",
        encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_PATH", yml)
    s = cfg.load_settings()
    assert s.platforms["kuaishou"].cover_title is True


# ---------- 2. vision 打分择优 ----------

def _settings(has_key=True):
    s = MagicMock()
    s.vision_api_key = "k" if has_key else ""
    s.ai_api_key = "k" if has_key else ""
    return s


def test_score_candidates_returns_none_without_key():
    assert ks._score_cover_candidates([Path("a.jpg")], None) is None
    assert ks._score_cover_candidates([Path("a.jpg")], _settings(False)) is None


def test_score_candidates_uses_vision_rank(tmp_path):
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    with patch("bot.ai.cover_pick._vision_rank",
               return_value=[(b, 9.0, False), (a, 4.0, False)]):
        out = ks._score_cover_candidates([a, b], _settings())
    assert out == (b, 9.0)


def test_score_candidates_swallows_vision_error(tmp_path):
    a = tmp_path / "a.jpg"
    a.write_bytes(b"x")
    with patch("bot.ai.cover_pick._vision_rank", side_effect=RuntimeError("boom")):
        assert ks._score_cover_candidates([a], _settings()) is None


def test_pick_uses_vision_not_brightness(tmp_path):
    """vision 说自选封面更好时就选自选，即使推荐封面更亮。

    这是需求 2 的核心：不能只按亮度选。
    """
    self_cover = tmp_path / "self.jpg"
    self_cover.write_bytes(b"x" * 5000)
    reco = tmp_path / "reco_0.jpg"
    reco.write_bytes(b"y" * 5000)

    page = MagicMock()
    # 推荐更亮（150 > 60），但 vision 给自选更高分
    luma = {"self.jpg": 60.0, "reco_0.jpg": 150.0}

    with patch.object(ks, "_wait_recommend_covers", return_value=["https://cdn/0.jpg"]), \
         patch.object(ks, "_download_cover_urls", return_value=[reco]), \
         patch.object(ks, "_upload_cover"), \
         patch.object(ks, "_apply_recommend_cover") as ap, \
         patch.object(ks, "_score_cover_candidates",
                      return_value=(self_cover, 9.2)), \
         patch("bot.media.ffmpeg.mean_luma",
               side_effect=lambda p: luma.get(Path(p).name, 80.0)):
        out = ks._pick_best_cover(page, self_cover, title="", s=_settings())

    assert out == "self", "vision 判自选更好时不能因为更亮就换推荐"
    ap.assert_not_called()


def test_pick_uses_recommend_when_vision_prefers_it(tmp_path):
    self_cover = tmp_path / "self.jpg"
    self_cover.write_bytes(b"x" * 5000)
    reco = tmp_path / "reco_1.jpg"
    reco.write_bytes(b"y" * 5000)

    page = MagicMock()
    with patch.object(ks, "_wait_recommend_covers",
                      return_value=["https://cdn/0.jpg", "https://cdn/1.jpg"]), \
         patch.object(ks, "_download_cover_urls", return_value=[reco]), \
         patch.object(ks, "_upload_cover"), \
         patch.object(ks, "_apply_recommend_cover", return_value=True) as ap, \
         patch.object(ks, "_score_cover_candidates",
                      return_value=(reco, 9.8)), \
         patch("bot.media.ffmpeg.mean_luma", return_value=90.0):
        out = ks._pick_best_cover(page, self_cover, title="", s=_settings())

    assert out == "recommend"
    ap.assert_called_once()


def test_pick_excludes_dark_reco_before_scoring(tmp_path):
    """过暗的推荐封面在打分前就被排除（评分模型对暗图常给高「信息量」分）。"""
    self_cover = tmp_path / "self.jpg"
    self_cover.write_bytes(b"x" * 5000)
    dark = tmp_path / "reco_0.jpg"
    dark.write_bytes(b"y" * 5000)

    page = MagicMock()
    captured = {}

    def fake_score(files, s):
        captured["files"] = [Path(f).name for f in files]
        return (Path(files[0]), 8.0)

    with patch.object(ks, "_wait_recommend_covers", return_value=["https://cdn/0.jpg"]), \
         patch.object(ks, "_download_cover_urls", return_value=[dark]), \
         patch.object(ks, "_upload_cover"), \
         patch.object(ks, "_score_cover_candidates", side_effect=fake_score), \
         patch("bot.media.ffmpeg.mean_luma",
               side_effect=lambda p: 10.0 if "reco" in Path(p).name else 90.0):
        out = ks._pick_best_cover(page, self_cover, title="", s=_settings())

    assert out == "self"
    # 过暗的推荐封面不能进入打分候选
    assert "reco_0.jpg" not in captured.get("files", [])
    assert "self.jpg" in captured.get("files", [])


def test_pick_falls_back_to_brightness_without_vision(tmp_path):
    """vision 不可用时退化为比亮度（不能直接放弃推荐封面）。"""
    self_cover = tmp_path / "self.jpg"
    self_cover.write_bytes(b"x" * 5000)
    reco = tmp_path / "reco_0.jpg"
    reco.write_bytes(b"y" * 5000)

    page = MagicMock()
    luma = {"self.jpg": 50.0, "reco_0.jpg": 140.0}
    with patch.object(ks, "_wait_recommend_covers", return_value=["https://cdn/0.jpg"]), \
         patch.object(ks, "_download_cover_urls", return_value=[reco]), \
         patch.object(ks, "_upload_cover"), \
         patch.object(ks, "_apply_recommend_cover", return_value=True) as ap, \
         patch.object(ks, "_score_cover_candidates", return_value=None), \
         patch("bot.media.ffmpeg.mean_luma",
               side_effect=lambda p: luma.get(Path(p).name, 80.0)):
        out = ks._pick_best_cover(page, self_cover, title="", s=_settings(False))

    assert out == "recommend"
    ap.assert_called_once()

"""回归测试：封面与平台「智能推荐封面」对比择优（2026-11-05）。

实测结论（aliyun-gz 实机）：
- 推荐封面只在视频转码完成后出现，图未就绪时 div 带 _loading_ 类、img.src 为空；
- 加载完成后 img.src 是可下载的 CDN 地址（720x1280）；
- 用 Playwright 原生 click() 点击会真正切换主封面（blob URL 变化）；
- 推荐封面是平台原图，无法叠标题大字。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bot.publish import kuaishou as ks


def _page_with_state(states):
    """构造一个 page mock：evaluate(_RECO_STATE_JS) 依次返回 states。"""
    page = MagicMock()
    seq = list(states)

    def evaluate(js, *a):
        if "recommend-cover-item" in str(js):
            return seq.pop(0) if seq else []
        return "blob:before"

    page.evaluate.side_effect = evaluate
    return page


def test_wait_returns_empty_when_never_ready():
    page = _page_with_state([[{"i": 0, "loading": True, "src": ""}]] * 3)
    page.wait_for_timeout = lambda ms: None      # 不真等
    out = ks._wait_recommend_covers(page, timeout_sec=0.15)
    assert out == []


def test_wait_returns_urls_when_loaded():
    page = _page_with_state([[
        {"i": 0, "loading": False, "src": "https://cdn/a.jpg"},
        {"i": 1, "loading": False, "src": "https://cdn/b.jpg"},
    ]])
    out = ks._wait_recommend_covers(page, timeout_sec=5)
    assert out == ["https://cdn/a.jpg", "https://cdn/b.jpg"]


def test_wait_skips_while_loading():
    """有 loading 项时不能返回（此时 img.src 还是空的）。"""
    page = _page_with_state([
        [{"i": 0, "loading": True, "src": ""}],
        [{"i": 0, "loading": False, "src": "https://cdn/x.jpg"}],
    ])
    out = ks._wait_recommend_covers(page, timeout_sec=10)
    assert out == ["https://cdn/x.jpg"]


def test_download_filters_tiny_files(tmp_path):
    class Resp:
        status_code = 200
        content = b"x" * 100          # < 3000，视为坏图

    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return Resp()

    import httpx
    with patch.object(httpx, "Client", Client):
        out = ks._download_cover_urls(["https://cdn/a.jpg"], tmp_path)
    assert out == []


def test_apply_returns_false_when_not_enough_items():
    page = MagicMock()
    loc = MagicMock()
    loc.count.return_value = 1
    page.locator.return_value = loc
    assert ks._apply_recommend_cover(page, 3) is False


def test_pick_prefers_recommend_when_brighter(tmp_path):
    """推荐封面更亮 → 返回 recommend。"""
    self_cover = tmp_path / "self.jpg"
    self_cover.write_bytes(b"x" * 5000)

    page = MagicMock()
    luma = {"self.jpg": 45.0, "reco_0.jpg": 120.0}

    with patch.object(ks, "_wait_recommend_covers", return_value=["https://cdn/0.jpg"]), \
         patch.object(ks, "_download_cover_urls", return_value=[tmp_path / "reco_0.jpg"]), \
         patch.object(ks, "_upload_cover") as up, \
         patch.object(ks, "_apply_recommend_cover", return_value=True) as ap, \
         patch("bot.media.ffmpeg.mean_luma",
               side_effect=lambda p: luma.get(Path(p).name, 80.0)):
        out = ks._pick_best_cover(page, self_cover, title="")

    assert out == "recommend"
    up.assert_called_once()      # 先上传自选（保留标题大字能力）
    ap.assert_called_once_with(page, 0)


def test_pick_keeps_self_when_brighter(tmp_path):
    """自选封面更亮 → 不点推荐。"""
    self_cover = tmp_path / "self.jpg"
    self_cover.write_bytes(b"x" * 5000)

    page = MagicMock()
    luma = {"self.jpg": 130.0, "reco_0.jpg": 50.0}

    with patch.object(ks, "_wait_recommend_covers", return_value=["https://cdn/0.jpg"]), \
         patch.object(ks, "_download_cover_urls", return_value=[tmp_path / "reco_0.jpg"]), \
         patch.object(ks, "_upload_cover"), \
         patch.object(ks, "_apply_recommend_cover") as ap, \
         patch("bot.media.ffmpeg.mean_luma",
               side_effect=lambda p: luma.get(Path(p).name, 80.0)):
        out = ks._pick_best_cover(page, self_cover, title="")

    assert out == "self"
    ap.assert_not_called()


def test_pick_returns_none_when_both_missing():
    page = MagicMock()
    with patch.object(ks, "_wait_recommend_covers", return_value=[]), \
         patch.object(ks, "_download_cover_urls", return_value=[]):
        out = ks._pick_best_cover(page, None, title="")
    assert out == "none"


def test_pick_uses_recommend_when_no_self_cover(tmp_path):
    """没有自选封面时，推荐封面直接可用。"""
    page = MagicMock()
    with patch.object(ks, "_wait_recommend_covers", return_value=["https://cdn/0.jpg"]), \
         patch.object(ks, "_download_cover_urls", return_value=[tmp_path / "reco_0.jpg"]), \
         patch.object(ks, "_upload_cover"), \
         patch.object(ks, "_apply_recommend_cover", return_value=True), \
         patch("bot.media.ffmpeg.mean_luma", return_value=100.0):
        out = ks._pick_best_cover(page, None, title="")
    assert out == "recommend"


def test_pick_falls_back_to_self_when_click_fails(tmp_path):
    """点击推荐失败时不能丢掉自选封面。"""
    self_cover = tmp_path / "self.jpg"
    self_cover.write_bytes(b"x" * 5000)
    page = MagicMock()

    with patch.object(ks, "_wait_recommend_covers", return_value=["https://cdn/0.jpg"]), \
         patch.object(ks, "_download_cover_urls", return_value=[tmp_path / "reco_0.jpg"]), \
         patch.object(ks, "_upload_cover"), \
         patch.object(ks, "_apply_recommend_cover", return_value=False), \
         patch("bot.media.ffmpeg.mean_luma",
               side_effect=lambda p: 120.0 if "reco" in Path(p).name else 40.0):
        out = ks._pick_best_cover(page, self_cover, title="")
    assert out == "self"

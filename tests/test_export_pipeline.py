"""出海前置链路测试：国内素材清洗 + 短链展开 + 账号配方。

覆盖「错了就会露馅」的点：
1. 解压类账号绝不能剥音轨（ASMR 是内容本身）；
2. 动画类必须剥音轨（中文配音在海外是减分项）；
3. 中文硬字幕区必须裁掉，且裁完要拉回原分辨率；
4. 短链（v.douyin.com 等）必须展开，否则 yt-dlp 直接 Unsupported URL；
5. 出海任务绝不能烧中文字幕。
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot.media import export as ex


class TestExportProfile:
    def test_satisfying_never_strips_audio(self):
        """解压号的 ASMR 原声就是内容本体，剥掉等于毁片。"""
        prof = ex.ExportProfile.for_account("ig_satisfying")
        assert prof.strip_audio is False

    def test_anim_strips_audio(self):
        """3D/AI 动画多为中文旁白，海外无意义且暴露搬运痕迹。"""
        prof = ex.ExportProfile.for_account("ig_anim")
        assert prof.strip_audio is True

    def test_pets_and_home_keep_audio(self):
        for acct in ("ig_pets", "ig_home"):
            assert ex.ExportProfile.for_account(acct).strip_audio is False

    def test_satisfying_crops_less_than_anim(self):
        """解压类细节常在画面下部，裁切要保守。"""
        sat = ex.ExportProfile.for_account("ig_satisfying")
        anim = ex.ExportProfile.for_account("ig_anim")
        assert sat.crop_bottom_ratio < anim.crop_bottom_ratio

    def test_unknown_account_gets_safe_default(self):
        prof = ex.ExportProfile.for_account("unknown_acct")
        assert prof.name == "unknown_acct"
        assert prof.strip_audio is False, "未知账号默认保声（宁可留噪声也别误删）"


class TestExportDetection:
    def test_ig_targets_detected(self):
        assert ex.is_export_task("ig_pets")
        assert ex.is_export_task("kuaishou,ig_anim")
        assert ex.is_export_task(" ig_satisfying , douyin ")

    def test_domestic_only_not_export(self):
        assert not ex.is_export_task("kuaishou")
        assert not ex.is_export_task("kuaishou,douyin")
        assert not ex.is_export_task(None)
        assert not ex.is_export_task("")

    def test_export_account_extracted(self):
        assert ex.export_account_of("kuaishou,ig_home") == "ig_home"
        assert ex.export_account_of("ig_pets") == "ig_pets"
        assert ex.export_account_of("kuaishou") == ""


class TestCropFilter:
    def test_crop_and_restore_original_size(self):
        """裁掉底部字幕区后必须放大回原尺寸，否则 IG 会补黑边。"""
        import random
        vf, params = ex._crop_and_scale_filter(1080, 1920, 0.10, random.Random(42))
        assert "crop=1080:" in vf
        assert "scale=1080:1920" in vf, "必须 scale 回原分辨率"
        assert params["crop_bottom_ratio"] > 0

    def test_crop_height_is_even(self):
        """h264 要求宽高为偶数，奇数高度会编码失败。"""
        import random
        for seed in range(10):
            _, params = ex._crop_and_scale_filter(720, 1281, 0.10, random.Random(seed))
            assert params.get("keep_h", 2) % 2 == 0

    def test_zero_ratio_yields_no_filter(self):
        import random
        vf, params = ex._crop_and_scale_filter(1080, 1920, 0.0, random.Random(1))
        assert vf == ""
        assert params["crop_bottom_ratio"] == 0.0

    def test_jitter_varies_between_videos(self):
        """裁切比例要有随机抖动，避免整批视频切口一模一样。"""
        import random
        ratios = {ex._crop_and_scale_filter(1080, 1920, 0.10, random.Random(s))[1]
                  ["crop_bottom_ratio"] for s in range(12)}
        assert len(ratios) > 3, "抖动不足，容易触发同质化判定"

    def test_ratio_is_clamped(self):
        """离谱的输入（如 0.9）不能把画面裁没。"""
        import random
        _, params = ex._crop_and_scale_filter(1080, 1920, 0.9, random.Random(1))
        assert params["crop_bottom_ratio"] <= 0.25


class TestVerifyExport:
    def test_warns_on_landscape(self, tmp_path):
        dst = tmp_path / "out.mp4"
        dst.write_bytes(b"x" * 20_000)
        with patch.object(ex.ffmpeg, "video_info",
                          return_value={"width": 1920, "height": 1080,
                                        "duration": 20.0}):
            problems = ex.verify_export(tmp_path / "src.mp4", dst)
        assert any("横屏" in p for p in problems)

    def test_warns_on_too_long(self, tmp_path):
        dst = tmp_path / "out.mp4"
        dst.write_bytes(b"x" * 20_000)
        with patch.object(ex.ffmpeg, "video_info",
                          return_value={"width": 1080, "height": 1920,
                                        "duration": 180.0}):
            problems = ex.verify_export(tmp_path / "src.mp4", dst)
        assert any("90s" in p for p in problems)

    def test_warns_on_too_short(self, tmp_path):
        dst = tmp_path / "out.mp4"
        dst.write_bytes(b"x" * 20_000)
        with patch.object(ex.ffmpeg, "video_info",
                          return_value={"width": 1080, "height": 1920,
                                        "duration": 1.5}):
            problems = ex.verify_export(tmp_path / "src.mp4", dst)
        assert any("太短" in p for p in problems)

    def test_clean_vertical_video_passes(self, tmp_path):
        dst = tmp_path / "out.mp4"
        dst.write_bytes(b"x" * 20_000)
        with patch.object(ex.ffmpeg, "video_info",
                          return_value={"width": 1080, "height": 1920,
                                        "duration": 25.0}):
            problems = ex.verify_export(tmp_path / "src.mp4", dst)
        assert problems == []

    def test_missing_file_reported(self, tmp_path):
        problems = ex.verify_export(tmp_path / "src.mp4", tmp_path / "nope.mp4")
        assert problems


class TestShortLinkExpansion:
    def test_extract_url_from_douyin_share_text(self):
        """国内分享文案是整段话，必须先抠出 URL 再喂 yt-dlp。"""
        text = ("7.89 abc:/ 复制打开抖音，看看【某某的作品】超治愈的画面 "
                "https://v.douyin.com/iABC123/ 复制此链接")
        url = ex_url_from_text(text)
        assert url == "https://v.douyin.com/iABC123/"

    def test_extract_url_from_xhs_share_text(self):
        text = "67 某某发布了一篇小红书笔记，快来看吧！😆 xhslink.com/a/AbCdEf 复制本条信息"
        # 没有 http 前缀时正则抓不到——这是已知边界，见下一条测试
        assert ex_url_from_text(text) == ""

    def test_extract_returns_empty_when_no_url(self):
        assert ex_url_from_text("今天天气不错") == ""
        assert ex_url_from_text("") == ""

    def test_non_shortlink_passes_through(self):
        from bot.source.downloader import expand_short_link
        url = "https://www.youtube.com/watch?v=abc"
        final, platform = expand_short_link(url)
        assert final == url
        assert platform == ""

    def test_shortlink_host_detected(self):
        from bot.source.downloader import SHORT_LINK_HOSTS
        assert "v.douyin.com" in SHORT_LINK_HOSTS
        assert "xhslink.com" in SHORT_LINK_HOSTS
        assert "b23.tv" in SHORT_LINK_HOSTS

    def test_shortlink_expansion_follows_redirect(self):
        from bot.source.downloader import expand_short_link
        with patch("httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_client
            mock_resp = MagicMock()
            mock_resp.url = "https://www.douyin.com/video/7300000000000000000"
            mock_client.get.return_value = mock_resp

            final, platform = expand_short_link("https://v.douyin.com/iABC/")
        assert platform == "douyin"
        assert "douyin.com/video/" in final

    def test_expansion_failure_falls_back_to_original(self):
        from bot.source.downloader import expand_short_link
        with patch("httpx.Client", side_effect=Exception("network down")):
            final, platform = expand_short_link("https://v.douyin.com/iABC/")
        assert final == "https://v.douyin.com/iABC/"
        assert platform == "douyin"


def ex_url_from_text(text: str) -> str:
    from bot.source.downloader import extract_url_from_text
    return extract_url_from_text(text)


class TestPlatformMap:
    def test_domestic_platforms_mapped(self):
        from bot.source.downloader import _PLATFORM_MAP
        assert _PLATFORM_MAP["Douyin"] == "douyin"
        assert _PLATFORM_MAP["XiaoHongShu"] == "xhs"

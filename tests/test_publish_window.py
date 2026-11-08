"""发布闸门测试：平台级窗口/日限/间隔覆盖。

背景（2026-11-09）：IG 矩阵号上线时发现全局发布窗口 10:00~22:00 是北京时间，
对欧美受众完全错位（会把内容发到他们深夜）。因此加了平台级 window 覆盖。
这组测试锁定三件事：
1. 平台配了 window 就用平台的，不配就用全局；
2. 跨夜窗口（20:00~06:00）的边界判断正确；
3. 日限/间隔的平台覆盖依然生效，且不影响未配置的平台。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from bot.main import _parse_hhmm, _window_only_gate, publish_gate


def _settings_with(platform_window=None, global_window=("10:00", "22:00")):
    from bot.config import PlatformConfig, PublishConfig, Settings

    s = Settings()
    s.publish = PublishConfig(daily_limit=6, min_gap_hours=2, window=global_window)
    s.platforms = {
        "kuaishou": PlatformConfig(enabled=True),
        "ig_pets": PlatformConfig(enabled=True, default=False,
                                  daily_limit=2, min_gap_hours=6,
                                  window=platform_window),
    }
    return s


class _FakeDb:
    """最小 DB 替身：只实现 gate 用到的两个方法。"""

    def __init__(self, count=0, anchor=None):
        self._count = count
        self._anchor = anchor

    def count_published_since(self, since, platform):
        return self._count

    def last_publish_anchor_at(self, platform):
        return self._anchor


class TestPlatformWindow:
    def test_platform_window_used_when_configured(self):
        """IG 配了 20:00~06:00，北京时间凌晨 1 点应当放行。"""
        s = _settings_with(platform_window=("20:00", "06:00"))
        with patch("bot.main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 9, 1, 30)
            mock_dt.fromisoformat = datetime.fromisoformat
            result = publish_gate(s, _FakeDb(), "ig_pets")
        assert result is None, f"凌晨 1 点应放行，实际：{result}"

    def test_platform_window_blocks_outside(self):
        """IG 窗口外（北京时间中午 12 点）必须拦截。"""
        s = _settings_with(platform_window=("20:00", "06:00"))
        with patch("bot.main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 9, 12, 0)
            mock_dt.fromisoformat = datetime.fromisoformat
            result = publish_gate(s, _FakeDb(), "ig_pets")
        assert result and "不在发布窗口" in result

    def test_global_window_used_when_platform_has_none(self):
        """快手没配 window，应回退全局 10:00~22:00。"""
        s = _settings_with(platform_window=("20:00", "06:00"))
        with patch("bot.main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 9, 12, 0)
            mock_dt.fromisoformat = datetime.fromisoformat
            result = publish_gate(s, _FakeDb(), "kuaishou")
        assert result is None, f"快手 12 点应在窗口内，实际：{result}"

    def test_global_window_still_blocks_kuaishou_at_night(self):
        """快手深夜仍要被全局窗口拦住（IG 的覆盖不能污染国内）。"""
        s = _settings_with(platform_window=("20:00", "06:00"))
        with patch("bot.main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 9, 1, 30)
            mock_dt.fromisoformat = datetime.fromisoformat
            result = publish_gate(s, _FakeDb(), "kuaishou")
        assert result and "不在发布窗口" in result


class TestNormalWindowBoundaries:
    def test_inside_normal_window(self):
        s = _settings_with(platform_window=("20:00", "06:00"))
        with patch("bot.main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 9, 15, 0)
            mock_dt.fromisoformat = datetime.fromisoformat
            assert publish_gate(s, _FakeDb(), "kuaishou") is None

    def test_before_window(self):
        s = _settings_with(platform_window=("20:00", "06:00"))
        with patch("bot.main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 9, 9, 59)
            mock_dt.fromisoformat = datetime.fromisoformat
            r = publish_gate(s, _FakeDb(), "kuaishou")
        assert r and "不在发布窗口" in r

    def test_at_window_end_is_blocked(self):
        """窗口是左闭右开：22:00 整点不应放行。"""
        s = _settings_with(platform_window=("20:00", "06:00"))
        with patch("bot.main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 9, 22, 0)
            mock_dt.fromisoformat = datetime.fromisoformat
            r = publish_gate(s, _FakeDb(), "kuaishou")
        assert r and "不在发布窗口" in r


class TestCrossMidnightWindow:
    def test_late_night_inside_cross_midnight(self):
        """20:00~06:00 是跨夜窗口，23:30 必须算在窗口内。"""
        s = _settings_with(platform_window=("20:00", "06:00"))
        with patch("bot.main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 9, 23, 30)
            mock_dt.fromisoformat = datetime.fromisoformat
            assert publish_gate(s, _FakeDb(), "ig_pets") is None

    def test_early_morning_inside_cross_midnight(self):
        s = _settings_with(platform_window=("20:00", "06:00"))
        with patch("bot.main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 9, 5, 59)
            mock_dt.fromisoformat = datetime.fromisoformat
            assert publish_gate(s, _FakeDb(), "ig_pets") is None

    def test_at_cross_start_boundary(self):
        s = _settings_with(platform_window=("20:00", "06:00"))
        with patch("bot.main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 9, 20, 0)
            mock_dt.fromisoformat = datetime.fromisoformat
            assert publish_gate(s, _FakeDb(), "ig_pets") is None

    def test_at_cross_end_boundary_blocked(self):
        s = _settings_with(platform_window=("20:00", "06:00"))
        with patch("bot.main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 9, 6, 0)
            mock_dt.fromisoformat = datetime.fromisoformat
            assert publish_gate(s, _FakeDb(), "ig_pets") is not None


class TestRateLimits:
    def test_platform_daily_limit_applies(self):
        s = _settings_with(platform_window=("20:00", "06:00"))
        with patch("bot.main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 9, 22, 0)
            mock_dt.fromisoformat = datetime.fromisoformat
            r = publish_gate(s, _FakeDb(count=2), "ig_pets")
        assert r and "每日发布上限 2" in r

    def test_platform_gap_applies(self):
        s = _settings_with(platform_window=("20:00", "06:00"))
        anchor = (datetime(2026, 11, 9, 21, 0)).isoformat(timespec="seconds")
        with patch("bot.main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 9, 22, 0)
            mock_dt.fromisoformat = datetime.fromisoformat
            r = publish_gate(s, _FakeDb(count=1, anchor=anchor), "ig_pets")
        assert r and "间隔不足" in r

    def test_disabled_platform_blocked(self):
        s = _settings_with()
        s.platforms["ig_pets"].enabled = False
        r = publish_gate(s, _FakeDb(), "ig_pets")
        assert r and "已停用" in r


class TestParseHHMM:
    def test_basic(self):
        assert _parse_hhmm("10:00") == 600
        assert _parse_hhmm("00:30") == 30
        assert _parse_hhmm("23:59") == 1439

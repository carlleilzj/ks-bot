"""TG 加源账号：必须显式指定赛道（A/B 两个号定位不同）。

背景（2026-11-09）
----------------
A 号是搞笑动画/萌宠号，B 号是家居带货号（四件套 + 女性睡衣）。
两者的白名单账号完全不能混 —— 把动物号塞进家居池会让 B 号收到
动物视频，反之亦然，而且很难事后发现（素材都是自动入库的）。

旧实现把裸链接默认加到 kuaishou2，用户发一个 A 号的源账号也会进 B 号池。
现在必须显式指定 @快手 或 @快手2，不指定就反问，绝不猜。
"""

from __future__ import annotations

import pytest

from bot.source.telegram_listener import (
    _looks_like_video_url,
    _parse_lane_marker,
)
from bot.source.source_accounts import parse_account_ref


LANES = ["kuaishou", "kuaishou2", "ig_pets", "ig_satisfying", "ig_anim", "ig_home"]


class TestLaneMarker:
    """@标记 解析：与投链的 @平台 语法一致，用户不用记两套。"""

    def test_overseas_aliases(self):
        """出海 4 大账号标记测试。"""
        for marker in ("@ig家居", "@ig好物", "@ig带货", "@ig_home", "@ighome", "@家居ig"):
            assert _parse_lane_marker(f"https://www.instagram.com/xxx/ {marker}", LANES)[0] == "ig_home"

        for marker in ("@ig萌宠", "@ig宠物", "@ig_pets", "@igpets", "@ig"):
            assert _parse_lane_marker(f"https://www.instagram.com/xxx/ {marker}", LANES)[0] == "ig_pets"

        for marker in ("@ig解压", "@ig治愈", "@ig_satisfying", "@igsatisfying", "@解压ig"):
            assert _parse_lane_marker(f"https://www.instagram.com/xxx/ {marker}", LANES)[0] == "ig_satisfying"

        for marker in ("@ig动画", "@ig_anim", "@iganim", "@动画ig"):
            assert _parse_lane_marker(f"https://www.instagram.com/xxx/ {marker}", LANES)[0] == "ig_anim"

    def test_chinese_alias_b2(self):
        lane, unknown = _parse_lane_marker(
            "https://www.instagram.com/harun_home/ @快手2", LANES)
        assert lane == "kuaishou2"
        assert unknown == []

    def test_chinese_alias_a(self):
        lane, _ = _parse_lane_marker(
            "https://www.instagram.com/xxx/ @快手", LANES)
        assert lane == "kuaishou"

    def test_english_alias(self):
        assert _parse_lane_marker("url @kuaishou2", LANES)[0] == "kuaishou2"
        assert _parse_lane_marker("url @ks2", LANES)[0] == "kuaishou2"
        assert _parse_lane_marker("url @ks", LANES)[0] == "kuaishou"

    def test_no_marker_returns_none(self):
        """没写标记必须返回 None —— 调用方会反问你选哪个，而不是猜。"""
        lane, _ = _parse_lane_marker(
            "https://www.instagram.com/harun_home/", LANES)
        assert lane is None

    def test_lane_not_enabled_is_unknown(self):
        """指定了没启用自动发现的平台 → 当作未识别，不静默忽略。"""
        lane, unknown = _parse_lane_marker("url @抖音", LANES)
        assert lane is None
        assert unknown and "未启用" in unknown[0]

    def test_garbage_marker(self):
        lane, unknown = _parse_lane_marker("url @随便写", LANES)
        assert lane is None
        assert unknown == ["随便写"]

    def test_first_valid_wins(self):
        lane, _ = _parse_lane_marker("url @快手2 @快手", LANES)
        assert lane == "kuaishou2"


class TestAccountRefWithMarker:
    """完整的「链接 + @标记」串要能正确拆出账号。"""

    def test_instagram_with_b2_marker(self):
        text = "https://www.instagram.com/harun_home/ @快手2"
        ref = parse_account_ref(text)
        assert ref == ("instagram", "harun_home",
                        "https://www.instagram.com/harun_home/")
        lane, _ = _parse_lane_marker(text, LANES)
        assert lane == "kuaishou2"

    def test_instagram_with_a_marker(self):
        text = "https://www.instagram.com/minimeownmeow/ @快手"
        ref = parse_account_ref(text)
        assert ref and ref[1] == "minimeownmeow"
        assert _parse_lane_marker(text, LANES)[0] == "kuaishou"

    def test_youtube_with_marker(self):
        text = "https://www.youtube.com/@FunnyAnimals @快手"
        ref = parse_account_ref(text)
        assert ref and ref[0] == "youtube" and ref[1] == "FunnyAnimals"
        assert _parse_lane_marker(text, LANES)[0] == "kuaishou"

    def test_reel_url_is_not_account(self):
        """单条 Reel 带 @标记 也不能被当成账号。"""
        text = "https://www.instagram.com/reel/ABC123/ @快手2"
        assert parse_account_ref(text) is None
        assert _looks_like_video_url(text) is True

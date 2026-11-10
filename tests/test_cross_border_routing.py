"""测试跨海搬运硬隔离法则（Cross-Border Routing Boundary）。

业务硬约束：
1. 出海链路（ig_*, youtube）：仅限国内跨平台素材（douyin, xhs, bilibili, kuaishou 等），严禁海外搬海外（如 IG 搬 IG）。
2. 国内链路（kuaishou, kuaishou2 等）：仅限海外优质素材（instagram, youtube, tiktok 等），严禁国内互搬。
"""

import pytest
from bot.source.cross_border import (
    validate_cross_border,
    filter_valid_targets,
    is_domestic_source,
    is_overseas_source,
    is_domestic_target,
    is_overseas_target,
)
from bot.source.downloader import VideoMeta
from bot.db import Database, State


class TestCrossBorderRules:
    def test_overseas_target_accepts_domestic_source(self):
        """出海平台接收国内素材（合规）。"""
        for sp in ("douyin", "xhs", "bilibili", "kuaishou", "toutiao"):
            for tp in ("ig_pets", "ig_satisfying", "ig_anim", "ig_home", "youtube"):
                ok, msg = validate_cross_border(sp, tp)
                assert ok is True, f"{sp} -> {tp} 应该是合规的"

    def test_overseas_target_rejects_overseas_source(self):
        """出海平台接收海外素材（严禁 IG 搬 IG / YouTube 搬 YouTube）。"""
        for sp in ("instagram", "youtube", "tiktok", "facebook"):
            for tp in ("ig_pets", "ig_satisfying", "ig_anim", "ig_home", "youtube"):
                ok, msg = validate_cross_border(sp, tp)
                assert ok is False, f"{sp} -> {tp} 必须被拦截"
                assert "禁止使用海外素材" in msg or "禁止 IG 搬 IG" in msg

    def test_domestic_target_accepts_overseas_source(self):
        """国内平台接收海外优质素材（合规）。"""
        for sp in ("instagram", "youtube", "tiktok", "facebook"):
            for tp in ("kuaishou", "kuaishou2"):
                ok, msg = validate_cross_border(sp, tp)
                assert ok is True, f"{sp} -> {tp} 应该是合规的"

    def test_domestic_target_rejects_domestic_source(self):
        """国内平台接收国内素材（严禁国内互搬）。"""
        for sp in ("douyin", "xhs", "bilibili", "kuaishou"):
            for tp in ("kuaishou", "kuaishou2"):
                ok, msg = validate_cross_border(sp, tp)
                assert ok is False, f"{sp} -> {tp} 必须被拦截"
                assert "禁止使用国内素材" in msg or "禁止国内同台搬运" in msg

    def test_filter_valid_targets_splits_properly(self):
        targets = ["ig_pets", "kuaishou", "youtube"]
        # 国内素材：只有 ig_pets 和 youtube 合法，kuaishou 违规
        valid, invalid = filter_valid_targets("douyin", targets)
        assert valid == ["ig_pets", "youtube"]
        assert invalid == ["kuaishou"]

        # 海外素材：只有 kuaishou 合法，ig_pets 和 youtube 违规
        valid2, invalid2 = filter_valid_targets("instagram", targets)
        assert valid2 == ["kuaishou"]
        assert invalid2 == ["ig_pets", "youtube"]


class TestDatabaseCrossBorderGate:
    def test_insert_video_rejects_violating_targets(self, tmp_path):
        db_path = tmp_path / "test.db"
        from bot.db import _SCHEMA
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_SCHEMA)
        conn.close()

        db = Database(path=db_path)
        meta = VideoMeta(
            source_url="https://www.instagram.com/reel/123/",
            platform="instagram",
            video_id="123",
            shortcode="instagram_123",
            username="test_user",
            title="test",
            caption="test",
            thumbnail_url="",
            duration=30.0,
            permalink="https://www.instagram.com/reel/123/",
        )
        # 尝试把 instagram 素材塞给出海平台 ig_pets -> 必须拦截抛出 ValueError
        with pytest.raises(ValueError) as exc:
            db.insert_video(meta, target_platforms=["ig_pets"])
        assert "违背跨海定向法则" in str(exc.value)

        # 把 instagram 素材塞给国内快手 -> 合规放行
        tid = db.insert_video(meta, target_platforms=["kuaishou"])
        assert tid is not None


class TestSourceAccountsCrossBorderGate:
    def test_add_account_blocks_cross_border_violation(self, tmp_path, monkeypatch):
        from bot.source import source_accounts as sa
        sa_file = tmp_path / "sa.json"
        monkeypatch.setattr(sa, "ACCOUNTS_FILE", sa_file)

        # 尝试给出海账号 ig_pets 添加 instagram 账号 -> 拦截
        ok, msg = sa.add_account("ig_pets", "instagram", "bad_creator")
        assert ok is False
        assert "跨海规则拦截" in msg

        # 尝试给国内快手添加 instagram 账号 -> 合规
        ok, msg = sa.add_account("kuaishou", "instagram", "good_creator")
        assert ok is True

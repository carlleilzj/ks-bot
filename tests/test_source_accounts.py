"""源账号白名单：解析、增删、发现层接入。

为什么需要白名单
--------------
关键词搜索覆盖面广但质量参差。已知的优质源账号（B 号 هارون هوم 中位 3206）
在历史数据里只留了个 username，用完就散。白名单把「哪个账号靠谱」沉淀下来。

为什么不做自动打分
----------------
2026-11-09 实测 B 号 43 个源账号：35 个只有 1 条样本，只有 5 个 >=3 条。
这么少的样本里，中位数会被单条爆款完全左右 ——
Lolly Pop 靠一条 14 万播放排到第一，而 n=8 的 هارون هوم 反被判「偏低」。
所以白名单以人工判断为主，本模块只提供「记住/查询」能力。
"""

from __future__ import annotations

import pytest

from bot.source import source_accounts as sa


@pytest.fixture(autouse=True)
def _no_whitelist(tmp_path, monkeypatch):
    """所有测试都不碰真实白名单文件。

    2026-11-09 踩过：线上部署时测试失败而本地通过，因为线上
    data/source_accounts.json 里有真实 IG 账号，run_discovery 的测试
    会读到它并真的去开 Playwright 抓 IG，把测试从 0.2 秒拖到 3 分钟。
    测试必须与运行数据完全隔离。
    """
    from bot.source import source_accounts as sa2
    monkeypatch.setattr(sa2, "ACCOUNTS_FILE", tmp_path / "no_such_src.json")


# ---------- 链接/用户名解析 ----------

class TestParseAccountRef:
    def test_instagram_profile(self):
        assert sa.parse_account_ref("https://www.instagram.com/harun_home/") == (
            "instagram", "harun_home", "https://www.instagram.com/harun_home/")

    def test_instagram_no_trailing_slash(self):
        assert sa.parse_account_ref("https://instagram.com/cilinea") == (
            "instagram", "cilinea", "https://www.instagram.com/cilinea/")

    def test_instagram_with_query(self):
        r = sa.parse_account_ref("https://www.instagram.com/cilinea/?hl=zh")
        assert r and r[1] == "cilinea"

    def test_instagram_reel_is_not_account(self):
        """单条 Reel 链接不能被当成账号 —— /reel 是保留路径。"""
        assert sa.parse_account_ref(
            "https://www.instagram.com/reel/DfcdLikuzia/") is None

    def test_instagram_post_path_is_not_account(self):
        assert sa.parse_account_ref("https://www.instagram.com/p/ABC123/") is None

    def test_instagram_stories_reserved(self):
        assert sa.parse_account_ref("https://www.instagram.com/stories/x/1/") is None

    def test_youtube_handle(self):
        assert sa.parse_account_ref("https://www.youtube.com/@HomeDecor") == (
            "youtube", "HomeDecor", "https://www.youtube.com/@HomeDecor")

    def test_youtube_channel_id(self):
        r = sa.parse_account_ref(
            "https://www.youtube.com/channel/UC3HP8a40b5n_WXot8qAHe5w")
        assert r and r[0] == "youtube"
        assert r[1] == "UC3HP8a40b5n_WXot8qAHe5w"

    def test_youtube_watch_is_not_account(self):
        assert sa.parse_account_ref(
            "https://www.youtube.com/watch?v=abc") is None

    def test_youtube_shorts_is_not_account(self):
        assert sa.parse_account_ref(
            "https://www.youtube.com/shorts/abc") is None

    def test_plain_handle(self):
        r = sa.parse_account_ref("@harun_home")
        assert r == ("", "harun_home", "")

    def test_plain_name(self):
        r = sa.parse_account_ref("cilinea")
        assert r == ("", "cilinea", "")

    def test_garbage(self):
        assert sa.parse_account_ref("这是中文不是账号") is None
        assert sa.parse_account_ref("") is None


# ---------- 增删查 ----------

class TestAccounts:
    def test_add_and_list(self):
        ok, msg = sa.add_account("kuaishou2", "instagram", "harun_home",
                                 "https://www.instagram.com/harun_home/", "测试")
        assert ok
        items = sa.accounts_for("kuaishou2", "instagram")
        assert len(items) == 1
        assert items[0]["account"] == "harun_home"

    def test_add_is_idempotent(self):
        sa.add_account("kuaishou2", "instagram", "cilinea")
        ok, msg = sa.add_account("kuaishou2", "instagram", "cilinea")
        assert not ok
        assert "已存在" in msg
        assert len(sa.accounts_for("kuaishou2", "instagram")) == 1

    def test_add_case_insensitive_dedupe(self):
        sa.add_account("kuaishou2", "instagram", "Cilinea")
        ok, _ = sa.add_account("kuaishou2", "instagram", "cilinea")
        assert not ok, "大小写不同应视为同一账号"
        assert len(sa.accounts_for("kuaishou2", "instagram")) == 1

    def test_add_updates_url_when_changed(self):
        sa.add_account("kuaishou2", "instagram", "x", url="old")
        ok, msg = sa.add_account("kuaishou2", "instagram", "x", url="new")
        assert not ok
        assert "已更新" in msg
        assert sa.accounts_for("kuaishou2", "instagram")[0]["url"] == "new"

    def test_remove(self):
        sa.add_account("kuaishou2", "instagram", "a")
        assert sa.remove_account("kuaishou2", "instagram", "a") is True
        assert sa.accounts_for("kuaishou2", "instagram") == []

    def test_remove_missing(self):
        assert sa.remove_account("kuaishou2", "instagram", "nope") is False

    def test_lanes_isolated(self):
        sa.add_account("kuaishou2", "instagram", "home_acct")
        sa.add_account("kuaishou", "youtube", "animal_chan")
        assert len(sa.accounts_for("kuaishou2", "instagram")) == 1
        assert len(sa.accounts_for("kuaishou2", "youtube")) == 0
        assert len(sa.accounts_for("kuaishou", "youtube")) == 1

    def test_corrupt_file_does_not_crash(self, tmp_path):
        sa.ACCOUNTS_FILE.write_text("not json{{{", encoding="utf-8")
        assert sa.list_accounts() == {}

    def test_missing_file_returns_empty(self):
        assert sa.list_accounts() == {}


# ---------- 预置 ----------

class TestSeed:
    def test_seed_when_empty(self):
        n = sa.seed_if_empty()
        assert n >= 1
        items = sa.accounts_for("kuaishou2", "instagram")
        assert any(i["account"] == "harun_home" for i in items)

    def test_seed_does_not_duplicate(self):
        sa.seed_if_empty()
        first = len(sa.accounts_for("kuaishou2", "instagram"))
        sa.seed_if_empty()
        assert len(sa.accounts_for("kuaishou2", "instagram")) == first

    def test_seed_skips_when_non_empty(self):
        sa.add_account("kuaishou", "youtube", "mine")
        assert sa.seed_if_empty() == 0


# ---------- 发现层接入 ----------

class TestWhitelistDiscovery:
    def _lane(self):
        return __import__("bot.source.discover", fromlist=["x"]).LaneConfig(
            name="kuaishou2", queries=["q"], require_any=["bed"],
            block_any=["makeup"], per_run=5)

    def test_whitelist_accounts_are_queried(self, monkeypatch):
        from bot.source import discover as dc
        sa.add_account("kuaishou2", "youtube", "HomeChan")
        seen = []

        def fake_channel(handle, limit):
            seen.append(handle)
            return [dc.Candidate(url="u1", title="cozy bedroom",
                                 source="youtube_whitelist")]
        monkeypatch.setitem(dc.WHITELIST_SOURCES, "youtube", fake_channel)
        out = dc.run_whitelist(self._lane(), "kuaishou2")
        assert "HomeChan" in seen
        assert len(out) == 1

    def test_whitelist_does_not_require_keyword_match(self, monkeypatch):
        """白名单账号本身垂直，标题没写品类词也不该丢。

        这正是白名单相对搜索的价值：搜索靠标题命中，白名单靠账号信任。
        """
        from bot.source import discover as dc
        sa.add_account("kuaishou2", "youtube", "chan")
        monkeypatch.setitem(dc.WHITELIST_SOURCES, "youtube",
                            lambda h, n: [dc.Candidate(url="u1",
                                                       title="新的一套，太好看了",
                                                       source="youtube_whitelist")])
        out = dc.run_whitelist(self._lane(), "kuaishou2")
        assert len(out) == 1

    def test_whitelist_still_blocks_excluded_content(self, monkeypatch):
        """带货/美妆即使来自白名单账号也要挡。"""
        from bot.source import discover as dc
        sa.add_account("kuaishou2", "youtube", "chan")
        monkeypatch.setitem(dc.WHITELIST_SOURCES, "youtube",
                            lambda h, n: [dc.Candidate(url="u1",
                                                       title="bedroom makeup tutorial",
                                                       source="youtube_whitelist")])
        assert dc.run_whitelist(self._lane(), "kuaishou2") == []

    def test_whitelist_respects_per_run(self, monkeypatch):
        from bot.source import discover as dc
        sa.add_account("kuaishou2", "youtube", "chan")
        monkeypatch.setitem(dc.WHITELIST_SOURCES, "youtube",
                            lambda h, n: [dc.Candidate(url=f"u{i}", title="bed",
                                                       source="youtube_whitelist")
                                          for i in range(10)])
        lane = self._lane()
        lane.per_run = 3
        assert len(dc.run_whitelist(lane, "kuaishou2")) == 3

    def test_empty_whitelist_returns_empty(self, monkeypatch):
        from bot.source import discover as dc
        assert dc.run_whitelist(self._lane(), "kuaishou2") == []


# ---------- 视频链接 vs 账号链接 ----------

class TestVideoUrlDetection:
    def test_video_urls(self):
        from bot.source.telegram_listener import _looks_like_video_url
        for u in [
            "https://www.instagram.com/reel/ABC/",
            "https://www.youtube.com/watch?v=abc",
            "https://youtu.be/abc",
            "https://www.youtube.com/shorts/abc",
            "https://www.douyin.com/video/123",
            "https://v.douyin.com/abc/",
            "https://www.bilibili.com/video/BV1xx",
        ]:
            assert _looks_like_video_url(u), u

    def test_account_urls_are_not_video(self):
        from bot.source.telegram_listener import _looks_like_video_url
        for u in [
            "https://www.instagram.com/harun_home/",
            "https://www.youtube.com/@HomeDecor",
            "https://www.youtube.com/channel/UCxxx",
        ]:
            assert not _looks_like_video_url(u), u

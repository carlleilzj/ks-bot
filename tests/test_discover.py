"""自动素材发现：赛道隔离与题材过滤。

动机（2026-11-09 实测数据）
--------------------------
B 号（家居）10-24~11-02 床品占 71%，11-03 之后掉到 45%：
    修眉先拉紧皮肤，手抖也不会刮秃        ← 美妆教程
    一脚踩中宝石蝴蝶，直接长出一双梦中情鞋   ← 女鞋
    谁能拒绝纯白电竞房                    ← 电竞房
人工挑素材会漂。本模块的价值就是**用关键词把漂移挡在入库之前**。

所以这里测的重点不是「能不能搜到视频」，而是
**会不会把不属于该赛道的内容放进来**。
"""

from __future__ import annotations

import pytest

from bot.source import discover as dc


def _cand(title="", desc="", dur=30.0, url="https://x/1"):
    return dc.Candidate(url=url, title=title, description=desc,
                        source="youtube", duration=dur)


HOME_LANE = dc.LaneConfig(
    name="kuaishou2",
    queries=["bedroom decor"],
    require_any=["bed", "bedding", "blanket", "duvet", "pillow", "sheet",
                 "sofa", "rug", "curtain", "bedroom", "mattress", "pyjama",
                 "四件套", "床品", "毛绒", "被", "枕"],
    block_any=["makeup", "eyebrow", "beauty tutorial", "gaming room",
               "pc build", "修眉", "化妆", "电竞"],
    per_run=8,
)

ANIMAL_LANE = dc.LaneConfig(
    name="kuaishou",
    queries=["funny animal animation"],
    require_any=[],
    per_run=8,
)


# ---------- 关键词匹配 ----------

class TestMatchKeywords:
    def test_english_case_insensitive(self):
        assert dc.match_keywords("COZY BEDROOM Decor", ["bedroom"]) == ["bedroom"]

    def test_chinese(self):
        assert dc.match_keywords("换上这套毛绒四件套", ["四件套", "毛绒"]) == ["四件套", "毛绒"]

    def test_no_match(self):
        assert dc.match_keywords("修眉教程", ["bedding"]) == []

    def test_empty_keywords(self):
        assert dc.match_keywords("anything", []) == []

    def test_non_string_items_are_skipped(self):
        """YAML 的 `- shop:` 会解析成 {'shop': None}，不能让它炸掉整条链路。"""
        assert dc.match_keywords("shop now", [{"shop": None}, "shop now"]) == ["shop now"]


# ---------- 赛道过滤 ----------

class TestEvaluate:
    def test_home_content_passes(self):
        ok, why = dc.evaluate(
            _cand("Cozy bedroom makeover with fluffy duvet"), HOME_LANE)
        assert ok, why

    def test_makeup_rejected(self):
        """B 号真实混入过的内容类型。"""
        ok, why = dc.evaluate(
            _cand("修眉先拉紧皮肤，手抖也不会刮秃"), HOME_LANE)
        assert not ok
        assert "排除词" in why or "关键词" in why

    def test_gaming_room_rejected(self):
        ok, why = dc.evaluate(
            _cand("谁能拒绝纯白电竞房，宅在里面根本不想出门"), HOME_LANE)
        assert not ok

    def test_gem_shoes_rejected(self):
        """「宝石蝴蝶鞋」本身含 bed？不含，但也不该通过 —— 验证 require_any 生效。"""
        ok, why = dc.evaluate(
            _cand("一脚踩中宝石蝴蝶，直接长出一双梦中情鞋"), HOME_LANE)
        assert not ok

    def test_makeup_inside_bedroom_rejected(self):
        """最容易漏的一种：标题里有 bedroom（命中 require），实际是美妆。

        block_any 优先级必须高于 require_any，否则「在卧室化妆」会混进来。
        """
        ok, why = dc.evaluate(
            _cand("Bedroom makeup tutorial: eyebrow shaping"), HOME_LANE)
        assert not ok, "排除词应压过命中词"
        assert "排除词" in why

    def test_duration_too_long_rejected(self):
        ok, why = dc.evaluate(_cand("bedroom tour", dur=600), HOME_LANE)
        assert not ok
        assert "时长" in why

    def test_duration_too_short_rejected(self):
        ok, why = dc.evaluate(_cand("bedroom", dur=1), HOME_LANE)
        assert not ok

    def test_zero_duration_allowed(self):
        """extract_flat 时常拿不到时长，不能因此全丢。"""
        ok, _ = dc.evaluate(_cand("cozy bedroom", dur=0), HOME_LANE)
        assert ok

    def test_lane_without_require_accepts_anything(self):
        ok, _ = dc.evaluate(_cand("随便什么内容"), ANIMAL_LANE)
        assert ok

    def test_records_matched_keywords(self):
        c = _cand("fluffy duvet and pillow set")
        dc.evaluate(c, HOME_LANE)
        assert set(c.matched_keywords) >= {"duvet", "pillow"}

    def test_empty_title_passes_only_when_allowed(self):
        """搜索阶段标题为空时放行到元数据阶段判断，但不配 allow 时仍拦截。

        ytsearch 偶发返回空 title；此时在搜索阶段判死会误杀正常素材，
        所以 run_lane 用 allow_empty_hint=True，最终闸门在元数据阶段。
        """
        c = _cand("", url="u1")
        ok, _ = dc.evaluate(c, HOME_LANE)
        assert not ok, "默认不允许空标题通过"
        ok2, why = dc.evaluate(c, HOME_LANE, allow_empty_hint=True)
        assert ok2
        assert "元数据" in why

    def test_empty_title_still_blocked_by_block_any(self):
        """即使允许空标题，命中排除词也不能放行（排除词优先）。"""
        c = _cand("makeup tutorial", url="u1")
        ok, why = dc.evaluate(c, HOME_LANE, allow_empty_hint=True)
        assert not ok
        assert "排除词" in why


# ---------- 跑一条 lane ----------

class TestRunLane:
    def test_filters_and_dedupes(self, monkeypatch):
        hits = [
            _cand("cozy bedroom decor", url="u1"),
            _cand("cozy bedroom decor", url="u1"),      # 重复
            _cand("修眉教程", url="u2"),                 # 应被过滤
            _cand("fluffy duvet bedroom", url="u3"),
        ]
        monkeypatch.setitem(dc.SOURCES, "youtube",
                            lambda q, n: list(hits))
        out = dc.run_lane(HOME_LANE, sources=["youtube"])
        urls = [c.url for c in out]
        assert urls == ["u1", "u3"], urls

    def test_respects_per_run(self, monkeypatch):
        hits = [_cand(f"cozy bedroom {i}", url=f"u{i}") for i in range(20)]
        monkeypatch.setitem(dc.SOURCES, "youtube", lambda q, n: list(hits))
        lane = dc.LaneConfig(name="t", queries=["q"], require_any=["bedroom"],
                             per_run=3)
        assert len(dc.run_lane(lane, sources=["youtube"])) == 3

    def test_unknown_source_is_skipped(self):
        lane = dc.LaneConfig(name="t", queries=["q"], per_run=1)
        assert dc.run_lane(lane, sources=["nope"]) == []

    def test_source_error_does_not_crash(self, monkeypatch):
        def boom(q, n):
            raise RuntimeError("网络断了")
        monkeypatch.setitem(dc.SOURCES, "youtube", boom)
        # run_lane 不捕获来源异常是设计选择：上层要能感知失败，
        # 但单个来源内部（discover_youtube）已自行 try/except。
        with pytest.raises(RuntimeError):
            dc.run_lane(HOME_LANE, sources=["youtube"])


# ---------- 来源实现 ----------

class TestYoutubeSource:
    def test_returns_empty_on_error(self, monkeypatch):
        import yt_dlp

        def boom(*a, **k):
            raise yt_dlp.DownloadError("网络不可达")
        monkeypatch.setattr(yt_dlp, "YoutubeDL", boom)
        assert dc.discover_youtube("anything") == []

    def test_parses_entries(self, monkeypatch):
        import yt_dlp

        class FakeYDL:
            def __init__(self, opts):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def extract_info(self, url, download=False):
                return {"entries": [
                    {"id": "abc", "title": "Cozy Bedroom", "duration": 30,
                     "channel": "HomeChan"},
                    {"id": "", "title": "no id"},
                ]}
        monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
        out = dc.discover_youtube("bedroom")
        assert len(out) == 1
        assert out[0].url == "https://www.youtube.com/watch?v=abc"
        assert out[0].source == "youtube"
        assert out[0].channel == "HomeChan"


class TestInstagramSource:
    def test_currently_returns_empty(self):
        """IG 发现当前不可用（缺 sessionid），必须安静返回空而不是抛异常。"""
        assert dc.discover_instagram("bedroom") == []


# ---------- 配置解析 ----------

class TestLaneFromConfig:
    def test_full_config(self):
        lane = dc.lane_from_config("kuaishou2", {
            "queries": ["q1", "q2"],
            "require_any": ["bed"],
            "block_any": ["makeup"],
            "per_run": 3,
        })
        assert lane.name == "kuaishou2"
        assert lane.queries == ["q1", "q2"]
        assert lane.require_any == ["bed"]
        assert lane.per_run == 3

    def test_bad_types_fall_back(self):
        """配置写错不能让发现链路崩掉。"""
        lane = dc.lane_from_config("x", {
            "queries": "not-a-list",
            "per_run": "abc",
            "min_duration": None,
        })
        assert lane.queries == []
        assert lane.per_run == 8
        assert lane.min_duration == 5.0

    def test_empty_config(self):
        lane = dc.lane_from_config("x", {})
        assert lane.per_run == 8 and lane.queries == []

    def test_sources_default(self):
        assert dc.sources_from_config({}) == ["youtube"]

    def test_sources_filters_unknown(self):
        assert dc.sources_from_config({"sources": ["youtube", "myspace"]}) == ["youtube"]

    def test_sources_all_unknown_falls_back(self):
        assert dc.sources_from_config({"sources": ["nope"]}) == ["youtube"]


class TestConfigYamlQueries:
    """真实 config.yaml 的查询词必须能被完整读出来。

    2026-11-09 踩过：YAML 里写 `- cozy bedroom decor #shorts`，
    `#` 被当成注释，查询词静默变成 "cozy bedroom decor"，`#shorts`
    后缀丢失 —— 而正是这个后缀决定能不能搜到短视频。
    这种错误不报错，只是效果变差，所以必须用测试钉住。
    """

    def _queries(self, platform):
        import yaml
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        data = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
        return data["platforms"][platform]["discover"]["queries"]

    def test_shorts_suffix_preserved_for_home_lane(self):
        qs = self._queries("kuaishou2")
        with_shorts = [q for q in qs if "#shorts" in q]
        assert len(with_shorts) >= 3, f"B 号查询词丢了 #shorts：{qs}"

    def test_shorts_suffix_preserved_for_animal_lane(self):
        qs = self._queries("kuaishou")
        with_shorts = [q for q in qs if "#shorts" in q]
        assert len(with_shorts) >= 3, f"A 号查询词丢了 #shorts：{qs}"

    def test_home_lane_has_genre_gate(self):
        """B 号必须有题材闸门，否则会重新漂移。"""
        import yaml
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        data = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
        conf = data["platforms"]["kuaishou2"]["discover"]
        assert conf.get("require_any"), "B 号缺少 require_any 题材闸门"
        assert conf.get("block_any"), "B 号缺少 block_any 排除词"
        # 曾经真实混入的内容类型必须在排除名单里
        blocked = " ".join(conf["block_any"]).lower()
        for word in ("makeup", "gaming", "修眉", "电竞"):
            assert word in blocked, f"排除词缺少 {word}"

    def test_block_any_are_all_strings(self):
        """YAML 里 `- shop:` 会被解析成 {'shop': None}，不是字符串。

        2026-11-09 踩过：写了 `- shop:` 想挡带货话术，结果变成 dict，
        下游 match_keywords 对着 dict 调 lower() 会炸。
        所以这里钉住「排除词必须全是字符串」，并要求带上带货相关词。
        """
        import yaml
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        data = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
        blocked = data["platforms"]["kuaishou2"]["discover"]["block_any"]
        bad = [x for x in blocked if not isinstance(x, str)]
        assert not bad, f"block_any 里有非字符串项（YAML 类型坑）：{bad}"
        # 带货/推广类：实测 YouTube 上大量「fluffy duvet」视频其实是 Amazon 联盟推广
        joined = " ".join(blocked).lower()
        for word in ("amazon", "amzn.to", "affiliate", "shop"):
            assert word in joined, f"排除词缺少带货关键词 {word}"


# ---------- 调度器 ----------

class _FakePlat:
    def __init__(self, discover):
        self.discover = discover


class _FakeSettings:
    def __init__(self, platforms):
        self.platforms = platforms


class _FakeDB:
    def __init__(self, existing=None):
        self.existing = set(existing or [])
        self.inserted = []

    def find_by_source_url(self, url):
        return {"id": 1} if url in self.existing else None

    def insert_video(self, meta, target_platforms=None, source_tag=""):
        self.inserted.append((meta.source_url, target_platforms, source_tag))
        return len(self.inserted)


def _meta(url, caption="cozy bedroom duvet", dur=30.0):
    return type("M", (), {
        "source_url": url, "shortcode": url, "caption": caption,
        "permalink": url, "username": "ch", "platform": "youtube",
        "duration": dur, "title": caption,
    })()


class TestRunDiscovery:
    def _settings(self, **over):
        conf = {"enabled": True, "interval_hours": 6, "per_query": 5,
                "per_run": 5, "sources": ["youtube"],
                "queries": ["bedroom"], "require_any": ["bed"],
                "block_any": ["makeup"]}
        conf.update(over)
        return _FakeSettings({"kuaishou2": _FakePlat(conf)})

    def test_disabled_lane_skipped(self, monkeypatch):
        monkeypatch.setattr(dc, "_last_run", {})
        s = _FakeSettings({"kuaishou2": _FakePlat({"enabled": False})})
        assert dc.run_discovery(s, _FakeDB(), force=True) == {}

    def test_lane_without_queries_skipped(self, monkeypatch):
        monkeypatch.setattr(dc, "_last_run", {})
        s = self._settings(queries=[])
        assert dc.run_discovery(s, _FakeDB(), force=True) == {}

    def test_inserts_matching_candidate(self, monkeypatch):
        monkeypatch.setattr(dc, "_last_run", {})
        monkeypatch.setitem(dc.SOURCES, "youtube",
                            lambda q, n: [dc.Candidate(url="u1",
                                                       title="cozy bedroom duvet",
                                                       source="youtube")])
        monkeypatch.setattr("bot.source.downloader.extract_meta",
                            lambda url: _meta(url))
        db = _FakeDB()
        out = dc.run_discovery(self._settings(), db, force=True)
        assert out["kuaishou2"]["new"] == 1
        assert db.inserted[0][1] == ["kuaishou2"], "必须只发到该 lane 的平台"
        assert db.inserted[0][2].startswith("auto:")

    def test_skips_already_known_url(self, monkeypatch):
        monkeypatch.setattr(dc, "_last_run", {})
        monkeypatch.setitem(dc.SOURCES, "youtube",
                            lambda q, n: [dc.Candidate(url="dup",
                                                       title="cozy bedroom",
                                                       source="youtube")])
        monkeypatch.setattr("bot.source.downloader.extract_meta",
                            lambda url: _meta(url))
        db = _FakeDB(existing={"dup"})
        out = dc.run_discovery(self._settings(), db, force=True)
        assert out["kuaishou2"]["new"] == 0
        assert out["kuaishou2"]["skipped"] == 1
        assert db.inserted == []

    def test_metadata_stage_filters_by_real_caption(self, monkeypatch):
        """搜索标题看着像家居，真实 caption 是美妆 —— 必须在元数据阶段拦住。

        这是 B 号漂移的真实成因：标题/频道名不可靠，content 才是准的。
        """
        monkeypatch.setattr(dc, "_last_run", {})
        monkeypatch.setitem(dc.SOURCES, "youtube",
                            lambda q, n: [dc.Candidate(url="u1",
                                                       title="cozy bedroom decor",
                                                       source="youtube")])
        monkeypatch.setattr("bot.source.downloader.extract_meta",
                            lambda url: _meta(url, caption="bedroom makeup tutorial eyebrow"))
        db = _FakeDB()
        out = dc.run_discovery(self._settings(), db, force=True)
        assert out["kuaishou2"]["new"] == 0
        assert db.inserted == []

    def test_rejects_candidate_without_genre_evidence(self, monkeypatch):
        """搜索标题和真实元数据都拿不到题材证据时丢弃。

        实测确有这种视频（caption 为空或只有外链）。B 号有 require_any 闸门，
        不能因为「不知道是什么」就放行 —— 那正是漂移的起点。
        """
        monkeypatch.setattr(dc, "_last_run", {})
        monkeypatch.setitem(dc.SOURCES, "youtube",
                            lambda q, n: [dc.Candidate(url="u1", title="",
                                                       source="youtube")])
        monkeypatch.setattr("bot.source.downloader.extract_meta",
                            lambda url: _meta(url, caption=""))
        db = _FakeDB()
        out = dc.run_discovery(self._settings(), db, force=True)
        assert out["kuaishou2"]["new"] == 0
        assert db.inserted == []

    def test_accepts_when_only_title_has_evidence(self, monkeypatch):
        """很多短视频只有标题、description 为空 —— 不能因此丢掉。

        实测 2026-11-09：一条「15+cozy bedroom decor ideas」视频
        description 是空串，只有 title 命中关键词。若校验只看 caption
        就会误杀；若不校验又会放进无关内容。正确做法是两个都看。
        """
        monkeypatch.setattr(dc, "_last_run", {})
        monkeypatch.setitem(dc.SOURCES, "youtube",
                            lambda q, n: [dc.Candidate(url="u1", title="",
                                                       source="youtube")])
        monkeypatch.setattr("bot.source.downloader.extract_meta",
                            lambda url: _meta(url, caption=""))
        # 元数据里的 title 有 evidence，但 _meta 的 title 默认等于 caption；
        # 这里显式构造只有 title 的 meta
        m = _meta("u1", caption="")
        m.title = "15+cozy bedroom decor ideas"
        monkeypatch.setattr("bot.source.downloader.extract_meta", lambda url: m)
        db = _FakeDB()
        out = dc.run_discovery(self._settings(), db, force=True)
        assert out["kuaishou2"]["new"] == 1

    def test_blank_caption_falls_back_to_title(self, monkeypatch):
        """入库时 caption 为空要用 title 兜底，否则下游拿不到任何文案线索。"""
        monkeypatch.setattr(dc, "_last_run", {})
        monkeypatch.setitem(dc.SOURCES, "youtube",
                            lambda q, n: [dc.Candidate(url="u1", title="",
                                                       source="youtube")])
        m = _meta("u1", caption="")
        m.title = "cozy bedroom duvet ideas"
        monkeypatch.setattr("bot.source.downloader.extract_meta", lambda url: m)

        updates = []

        class _DB(_FakeDB):
            def update(self, tid, **fields):
                updates.append(fields)

        dc.run_discovery(self._settings(), _DB(), force=True)
        assert updates and updates[0].get("caption") == "cozy bedroom duvet ideas"

    def test_one_bad_candidate_does_not_stop_batch(self, monkeypatch):
        monkeypatch.setattr(dc, "_last_run", {})
        monkeypatch.setitem(dc.SOURCES, "youtube",
                            lambda q, n: [
                                dc.Candidate(url="bad", title="cozy bedroom", source="youtube"),
                                dc.Candidate(url="good", title="cozy bedroom duvet", source="youtube"),
                            ])

        def _extract(url):
            if url == "bad":
                raise ValueError("解析失败")
            return _meta(url)
        monkeypatch.setattr("bot.source.downloader.extract_meta", _extract)
        db = _FakeDB()
        out = dc.run_discovery(self._settings(), db, force=True)
        assert out["kuaishou2"]["new"] == 1
        assert out["kuaishou2"]["errors"], "失败要记录，不能静默"

    def test_candidate_dropped_by_real_duration(self, monkeypatch):
        monkeypatch.setattr(dc, "_last_run", {})
        monkeypatch.setitem(dc.SOURCES, "youtube",
                            lambda q, n: [dc.Candidate(url="u1",
                                                       title="cozy bedroom",
                                                       source="youtube")])
        monkeypatch.setattr("bot.source.downloader.extract_meta",
                            lambda url: _meta(url, dur=900))
        db = _FakeDB()
        out = dc.run_discovery(self._settings(), db, force=True)
        assert out["kuaishou2"]["new"] == 0

    def test_respects_interval_unless_forced(self, monkeypatch):
        monkeypatch.setattr(dc, "_last_run", {"all": 1e18})   # 假装刚跑过
        monkeypatch.setattr("bot.source.downloader.extract_meta",
                            lambda url: _meta(url))
        s = self._settings()
        assert dc.run_discovery(s, _FakeDB()) == {}, "未到间隔应跳过"
        # force 时照跑
        monkeypatch.setitem(dc.SOURCES, "youtube",
                            lambda q, n: [dc.Candidate(url="u1",
                                                       title="cozy bedroom",
                                                       source="youtube")])
        assert dc.run_discovery(s, _FakeDB(), force=True)

"""回归测试：B站 出海寻源（一期）。

背景与约束（全部来自 2026-11-12 实测）：
  · B站 搜索接口在国内线路可用（阿里云 HTTP 200 / code=0），
    在境外协调端 gc-jp 被 412 风控 —— 所以发现必须在阿里云跑。
  · 连续请求会 412，间隔 3~5 秒恢复 → 必须带退避重试。
  · B站 返回的 duration 是 "12:26" 文本，不转秒就进不了时长闸门，
    而时长闸门是挡掉「204 分钟教程课」的唯一手段。
  · 关键词召回噪声大（「AI动画」全教程、「ASMR」吃播），
    必须靠中文 require_any + 时长区间 + block_any 三重过滤。
  · ig_satisfying 要求**真人现场**手工，必须用 block_any 排除 AI/动画。
"""

from __future__ import annotations

import pytest

from bot.db import Database, JobState, State
from bot.source import discover as d
from bot.source import discover_worker as w

# ---------- B站 时长解析 ----------

@pytest.mark.parametrize("raw,expect", [
    ("12:26", 746), ("1:02:33", 3753), ("0:36", 36), ("204:44", 12284),
    ("", 0), (None, 0), ("abc", 0), (59, 59), ("0:00", 0),
])
def test_parse_bili_duration(raw, expect):
    assert d._parse_bili_duration(raw) == expect


def test_parse_duration_handles_missing():
    """解析不出来返回 0：下游 evaluate 对 0 不做时长判定（交给元数据阶段）。"""
    assert d._parse_bili_duration("--:--") == 0


# ---------- 标题清理 ----------

def test_strip_html_removes_highlight():
    """B站 会把命中词包成 <em>；不剥离会让 require_any 匹配变得难预测。"""
    assert d._strip_html('3D<em class="keyword">动画</em>短片') == "3D动画短片"


def test_strip_html_handles_entities():
    assert d._strip_html("&quot;刀工&quot;") == '"刀工"'


# ---------- 赛道过滤（时长 / 排除词 / 必含词） ----------

def _cand(**kw):
    base = dict(url="https://www.bilibili.com/video/BV1", title="", description="",
                source="bilibili", channel="", duration=0.0,
                like_count=None, published_timestamp=0.0)
    base.update(kw)
    return d.Candidate(**base)


def test_plan_rejects_long_tutorial():
    """实测「AI动画」搜出 204 分钟教程课 —— 时长闸门必须挡住。"""
    plan = w.LanePlan(name="ig_anim", queries=["AI动画"], min_duration=15,
                      max_duration=180)
    ok, why = w.filter_candidate(_cand(title="AI动画教程", duration=204 * 60), plan)
    assert not ok and "时长" in why


def test_plan_rejects_too_short():
    plan = w.LanePlan(name="ig_anim", min_duration=15, max_duration=180)
    ok, why = w.filter_candidate(_cand(title="3D动画", duration=5), plan)
    assert not ok and "时长" in why


def test_plan_requires_genre_keyword():
    plan = w.LanePlan(name="ig_anim", require_any=["动画", "3d"])
    ok, why = w.filter_candidate(_cand(title="今天吃什么", duration=60), plan)
    assert not ok and "未命中" in why


def test_plan_accepts_matching_candidate():
    plan = w.LanePlan(name="ig_anim", require_any=["动画"], min_duration=15,
                      max_duration=180)
    ok, _ = w.filter_candidate(_cand(title="3D动画短片", duration=60), plan)
    assert ok


def test_satisfying_blocks_ai_and_animation():
    """ig_satisfying 要真人现场手工，AI/动画必须被 block_any 挡掉。"""
    # 与 config.yaml 里 ig_satisfying.block_any 保持一致
    plan = w.LanePlan(name="ig_satisfying",
                      require_any=["木工", "手工"],
                      block_any=["AI", "ai", "动画", "3D", "cg", "建模", "渲染",
                                 "虚拟", "游戏", "二次元", "漫剧"])
    for bad in ("AI生成的木工动画", "3D建模手工台", "木工CAD渲染演示"):
        ok, why = w.filter_candidate(_cand(title=bad, duration=60), plan)
        assert not ok, f"{bad} 不该通过"
        assert "排除词" in why


def test_satisfying_accepts_real_craft():
    """实测真实命中样本：『传统拉大锯解木』0:37，必须放行。"""
    plan = w.LanePlan(name="ig_satisfying",
                      require_any=["木工", "手工", "手艺", "榫卯", "雕刻", "解压",
                                   "微缩", "修复", "削", "肥皂", "木"],
                      block_any=["AI", "ai", "动画", "3D", "cg", "建模", "渲染",
                                 "虚拟", "游戏", "二次元", "漫剧"],
                      min_duration=15, max_duration=480)
    ok, _ = w.filter_candidate(
        _cand(title="传统拉大锯解木，众人合力破开粗壮大树桩#传统手艺", duration=37), plan)
    assert ok


def test_satisfying_rejects_short_ai_clip():
    """AI 生成的手工演示（标题常带 AI/建模/渲染）必须挡掉。"""
    plan = w.LanePlan(name="ig_satisfying",
                      require_any=["木工", "手工"],
                      block_any=["AI", "ai", "动画", "3D", "cg", "建模", "渲染"],
                      min_duration=15, max_duration=480)
    for bad in ("AI木工自动化演示", "3D建模木桌渲染", "手工动画教程"):
        ok, why = w.filter_candidate(_cand(title=bad, duration=60), plan)
        assert not ok, f"{bad} 不该通过"
        assert "排除词" in why


# ---------- 赛道计划从配置推导 ----------

class _Plat:
    def __init__(self, enabled, discover):
        self.enabled = enabled
        self.discover = discover
        self.categories = []          # step_copywrite 会读
        self.cover_title = False
        self.anchors = {}
        self.spark_task = False
        self.spark_task_title = ""
        self.attach_service = ""
        self.attach_prefer = ""
        self.attach_fallback = ""
        self.hot_topic = ""


class _S:
    def __init__(self, platforms, discovery=None):
        self.platforms = platforms
        self.discovery = discovery or {}


def test_lane_plans_only_overseas_enabled():
    s = _S({
        "ig_anim": _Plat(True, {"enabled": True, "queries": ["3D动画"],
                                "sources": ["bilibili"]}),
        "kuaishou": _Plat(True, {"enabled": True, "queries": ["动画"],
                                 "sources": ["bilibili"]}),  # 国内，不该被选
        "ig_home": _Plat(True, {"enabled": False, "queries": ["家居"]}),  # 未启用
    })
    plans = w.lane_plans(s)
    assert [p.name for p in plans] == ["ig_anim"]


def test_lane_plans_skip_without_queries():
    """没有 queries 的赛道不能生成空计划（否则每轮白空转）。"""
    s = _S({"ig_anim": _Plat(True, {"enabled": True, "queries": []})})
    assert w.lane_plans(s) == []


# ---------- 出海水位闸门独立于全局 ----------

def _db(tmp_path):
    return Database(tmp_path / "dw.db")


def test_overseas_backlog_counts_only_target_platforms(tmp_path):
    db = _db(tmp_path)
    cur = db.conn.execute(
        "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
        "VALUES ('a','a',?, '2026-01-01','2026-01-01')", (State.READY,))
    tid = cur.lastrowid
    db.conn.commit()
    db.create_publish_jobs(tid, ["kuaishou", "ig_anim", "ig_pets"])
    jobs = {j["platform"]: j for j in db.jobs(tid)}
    db.update_job(jobs["ig_anim"]["id"], state=JobState.PUBLISHED)
    db.update_job(jobs["ig_pets"]["id"], state=JobState.PUBLISHED)
    # 只剩 kuaishou 是 PENDING
    assert w.overseas_backlog(db, ["ig_anim", "ig_pets"]) == 0
    assert w.overseas_backlog(db, ["kuaishou"]) == 1
    db.close()


def test_overseas_watermark_independent_of_domestic_backlog(tmp_path):
    """国内积压顶到水位时，出海寻源不能被连带关掉。"""
    db = _db(tmp_path)
    cur = db.conn.execute(
        "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
        "VALUES ('k','k',?, '2026-01-01','2026-01-01')", (State.READY,))
    tid = cur.lastrowid
    db.conn.commit()
    db.create_publish_jobs(tid, ["kuaishou", "kuaishou2"])
    # 国内 2 条 PENDING；全局水位设 1 → 全局闸门会关，出海水位 8 → 不该关
    s = _S({"ig_anim": _Plat(True, {"enabled": True, "queries": ["3D动画"],
                                    "sources": ["bilibili"]})},
           discovery={"backlog_watermark": 1, "backlog_watermark_overseas": 8})
    s.remote_api_url = "http://x"
    s.remote_api_token = "t"

    called = {"n": 0}
    import bot.source.discover_worker as mod
    orig = mod.collect
    mod.collect = lambda p, dry_run=False: (called.update(n=called["n"] + 1) or [])
    try:
        mod.run_once(s, db=db)
    finally:
        mod.collect = orig
    assert called["n"] == 1, "国内积压不该关掉出海寻源"
    db.close()


def test_overseas_watermark_blocks_when_its_own_backlog_full(tmp_path):
    db = _db(tmp_path)
    cur = db.conn.execute(
        "INSERT INTO tasks (ig_media_id, shortcode, state, created_at, updated_at) "
        "VALUES ('o','o',?, '2026-01-01','2026-01-01')", (State.READY,))
    tid = cur.lastrowid
    db.conn.commit()
    db.create_publish_jobs(tid, ["ig_anim"])
    s = _S({"ig_anim": _Plat(True, {"enabled": True, "queries": ["3D动画"],
                                    "sources": ["bilibili"]})},
           discovery={"backlog_watermark_overseas": 1})
    s.remote_api_url = "http://x"
    s.remote_api_token = "t"

    called = {"n": 0}
    import bot.source.discover_worker as mod
    orig = mod.collect
    mod.collect = lambda p, dry_run=False: (called.update(n=called["n"] + 1) or [])
    try:
        assert mod.run_once(s, db=db) == {}
    finally:
        mod.collect = orig
    assert called["n"] == 0, "出海水位满时必须跳过寻源"
    db.close()


# ---------- 流水线级 block_any 复查 ----------

def _mk_task_db(tmp_path, caption, targets="ig_pets"):
    db = Database(tmp_path / "rc.db")
    cur = db.conn.execute(
        "INSERT INTO tasks (ig_media_id, shortcode, state, caption, target_platforms,"
        " created_at, updated_at) VALUES ('m','sc',?,?,?,'2026-01-01','2026-01-01')",
        (State.COPYWRITTEN, caption, targets))
    db.conn.commit()
    tid = cur.lastrowid
    db.create_publish_jobs(tid, [t for t in targets.split(",")])
    return db, tid


def _settings_with_block(targets, block):

    s = _S({t: _Plat(True, {"enabled": True, "block_any": block}) for t in targets})
    return s


def test_recheck_skips_furry_content(tmp_path):
    """存量 furry/兽装 任务必须被复查拦下（实测 t976-978 曾进入 READY）。"""
    import bot.main as main_mod

    db, tid = _mk_task_db(tmp_path, "【furry动画】被揉超大毛茸茸肚子的母胶兽兽人宠物")
    s = _settings_with_block(["ig_pets"], ["furry", "兽装", "兽人"])

    main_mod._recheck_block_any(s, db, db.get(tid), db.get(tid)["caption"])

    task = db.get(tid)
    assert task["state"] == State.SKIPPED
    assert "排除词" in (task["error"] or "")
    assert all(j["state"] == JobState.SKIPPED for j in db.jobs(tid)), \
        "待发 job 必须一并收成终态，否则仍占水位"
    db.close()


def test_recheck_skips_brand_ad(tmp_path):
    """存量品牌硬广（海尔/京东）也要拦（实测 t987-988）。"""
    import bot.main as main_mod

    db, tid = _mk_task_db(tmp_path, "海尔云溪冰箱630靠谱好物适配三代同堂", "ig_home")
    s = _settings_with_block(["ig_home"], ["海尔", "京东"])

    main_mod._recheck_block_any(s, db, db.get(tid), db.get(tid)["caption"])
    assert db.get(tid)["state"] == State.SKIPPED
    db.close()


def test_recheck_lets_clean_content_through(tmp_path):
    """真人木工内容不能被误杀（实测 t979 已正常发布）。"""
    import bot.main as main_mod

    db, tid = _mk_task_db(tmp_path, "传统拉大锯解木，众人合力破开粗壮大树桩", "ig_satisfying")
    s = _settings_with_block(["ig_satisfying"], ["AI", "动画", "3D"])

    main_mod._recheck_block_any(s, db, db.get(tid), db.get(tid)["caption"])
    assert db.get(tid)["state"] == State.COPYWRITTEN, "合规素材不能被改状态"
    db.close()


def test_recheck_checks_each_target_platform(tmp_path):
    """多平台任务：任一目标平台命中排除词就跳过。"""
    import bot.main as main_mod

    db, tid = _mk_task_db(tmp_path, "海尔空调好物", "ig_home,kuaishou")
    s = _settings_with_block(["ig_home"], ["海尔"])
    main_mod._recheck_block_any(s, db, db.get(tid), db.get(tid)["caption"])
    assert db.get(tid)["state"] == State.SKIPPED
    db.close()


def test_recheck_noop_without_caption(tmp_path):
    """没有 caption（如纯投链）时不误杀。"""
    import bot.main as main_mod

    db, tid = _mk_task_db(tmp_path, "", "ig_pets")
    s = _settings_with_block(["ig_pets"], ["furry"])
    main_mod._recheck_block_any(s, db, db.get(tid), "")
    assert db.get(tid)["state"] == State.COPYWRITTEN
    db.close()


def test_step_copywrite_actually_calls_recheck(tmp_path, monkeypatch):
    """接线测试：step_copywrite 必须真的调用复查。

    前面几个用例直接调 _recheck_block_any，只能证明函数本身对不对，
    证明不了它被接进流水线 —— 变异验证时把调用改成 pass，那些用例全绿。
    """
    import bot.main as main_mod

    db, tid = _mk_task_db(tmp_path, "【furry动画】兽人宠物", "ig_pets")
    s = _settings_with_block(["ig_pets"], ["furry", "兽人"])

    # 一旦走到 LLM 就说明没拦住 —— 让它在烂掉时炸得很明显
    called = {"llm": 0}
    monkeypatch.setattr(main_mod, "generate_copy",
                        lambda *a, **k: called.update(llm=called["llm"] + 1) or {})

    task = db.get(tid)
    main_mod.step_copywrite(s, db, task)

    assert db.get(tid)["state"] == State.SKIPPED
    assert called["llm"] == 0, "复查要在调 LLM 之前就拦住"
    db.close()

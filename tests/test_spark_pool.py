"""星火任务池变化跟踪（2026-10-15）。

收藏只能在快手 App 完成（PC 端无入口），所以自动收藏做不到。
这里只做「发现变化 → 推 TG 提醒」，人收到后去 App 收藏。
"""

import json

from bot.monitor import spark_pool


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(spark_pool, "POOL_PATH", tmp_path / "pool.json")


def test_first_run_records_without_alert(tmp_path, monkeypatch):
    """首次记录只建基线，不报"全部新增"（否则上线即刷屏）。"""
    _fresh(tmp_path, monkeypatch)
    sent = []
    res = spark_pool.note_candidates(
        ["任务A任务时间：2026.01.01-2027.01.01"],
        notify=sent.append)
    assert res["first_run"] is True
    assert res["added"] == []
    assert sent == []
    assert "任务A" in spark_pool.current_pool()


def test_detects_added_task(tmp_path, monkeypatch):
    """新任务上架 → 报新增。"""
    _fresh(tmp_path, monkeypatch)
    spark_pool.note_candidates(["任务A任务时间：x"], notify=lambda t: None)
    sent = []
    res = spark_pool.note_candidates(
        ["任务A任务时间：x", "榆树沟出了个辣媳妇1任务时间：2026.09.10-2027.09.10"],
        notify=sent.append)
    assert res["added"] == ["榆树沟出了个辣媳妇1"]
    assert res["removed"] == []
    assert len(sent) == 1 and "榆树沟出了个辣媳妇1" in sent[0]


def test_detects_removed_task(tmp_path, monkeypatch):
    """任务下架/停结算 → 报消失。"""
    _fresh(tmp_path, monkeypatch)
    spark_pool.note_candidates(
        ["任务A任务时间：x", "狐缘山间任务时间：y"], notify=lambda t: None)
    sent = []
    res = spark_pool.note_candidates(["任务A任务时间：x"], notify=sent.append)
    assert res["removed"] == ["狐缘山间"]
    assert len(sent) == 1 and "狐缘山间" in sent[0]


def test_no_change_no_alert(tmp_path, monkeypatch):
    """任务池没变 → 不推消息。"""
    _fresh(tmp_path, monkeypatch)
    titles = ["任务A任务时间：x", "任务B任务时间：y"]
    spark_pool.note_candidates(titles, notify=lambda t: None)
    sent = []
    res = spark_pool.note_candidates(titles, notify=sent.append)
    assert res == {"added": [], "removed": [], "first_run": False}
    assert sent == []


def test_empty_titles_ignored(tmp_path, monkeypatch):
    """空列表不覆盖已有基线（下拉偶发读空不应误报全部消失）。"""
    _fresh(tmp_path, monkeypatch)
    spark_pool.note_candidates(["任务A任务时间：x"], notify=lambda t: None)
    res = spark_pool.note_candidates([], notify=lambda t: None)
    assert res == {"added": [], "removed": [], "first_run": False}
    assert "任务A" in spark_pool.current_pool()


def test_suffix_stripped_for_comparison(tmp_path, monkeypatch):
    """任务时间后缀变化不算任务变化（同一任务延长周期不应误报）。"""
    _fresh(tmp_path, monkeypatch)
    spark_pool.note_candidates(["任务A任务时间：2026.01.01-2026.12.31"],
                               notify=lambda t: None)
    sent = []
    res = spark_pool.note_candidates(["任务A任务时间：2026.01.01-2027.12.31"],
                                     notify=sent.append)
    assert res["added"] == [] and res["removed"] == []
    assert sent == []

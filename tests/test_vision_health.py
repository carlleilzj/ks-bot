"""回归测试：视觉链路失败告警（2026-11-12 409 静默降级事故）。

事故回顾：把 AI_BASE_URL 换到另一家中转站、却留着旧家的 VISION_API_KEY，
三处视觉调用全部 403。所有失败都只是 WARNING，任务照常跑完发出，
几小时后才被「文案变得很泛」间接发现。画面探针/封面选优/真人检测
在此期间全部静默失效。

要锁住的语义：
  1. 连续失败到阈值才告警（偶发抖动不打扰）。
  2. **只告警一次** —— 挂一整夜不能刷上百条 TG。
  3. 各链独立计数（探针失败不该重置封面选优的计数）。
  4. 恢复后补一条通知，并把计数清零；再次恶化能重新告警。
"""

from __future__ import annotations

import pytest

from bot.ai import vision_health as vh


@pytest.fixture(autouse=True)
def _clean():
    vh.clear()
    sent: list[str] = []
    # 拦截真实 TG 推送，只记录
    import bot.ai.vision_health as mod

    orig = mod._notify
    mod._notify = lambda text: sent.append(text)
    yield sent
    mod._notify = orig
    vh.clear()


@pytest.fixture()
def sent(_clean):
    """把 autouse fixture 收集到的推送列表暴露成具名 fixture。"""
    return _clean


class _S:
    """最小 Settings 替身：vision_endpoint 会读这三个字段。"""

    ai_base_url = "https://cpa.example/v1"
    vision_base_url = "https://cpa.example/v1"
    vision_api_key = "sk-test-1234"
    ai_api_key = "sk-test-1234"


def test_no_alert_below_threshold(sent):
    """1~2 次失败不告警（避免偶发抖动打扰）。"""
    for _ in range(vh.ALERT_THRESHOLD - 1):
        vh.record_failure(_S(), "content_probe", "boom")
    assert sent == []
    assert vh.snapshot()["content_probe"]["failures"] == vh.ALERT_THRESHOLD - 1


def test_alert_on_threshold(sent):
    for _ in range(vh.ALERT_THRESHOLD):
        vh.record_failure(_S(), "content_probe", "403 Invalid API key")
    assert len(sent) == 1
    assert "content_probe" in sent[0]
    assert "403 Invalid API key" in sent[0]


def test_alert_only_once(sent):
    """挂一整夜（20 次失败）只发 1 条，不能刷屏。"""
    for _ in range(20):
        vh.record_failure(_S(), "content_probe", "boom")
    assert len(sent) == 1, f"应只告警一次，实际 {len(sent)} 条"


def test_alert_includes_endpoint_diagnostics(sent):
    """告警要带端点与 key 前缀 —— 端点/key 不同家是最常见的原因。"""
    for _ in range(vh.ALERT_THRESHOLD):
        vh.record_failure(_S(), "content_probe", "boom")
    assert "https://cpa.example/v1" in sent[0]
    assert "sk-test-" in sent[0]


def test_chains_count_independently(sent):
    """探针失败不应影响封面选优的计数。"""
    for _ in range(vh.ALERT_THRESHOLD - 1):
        vh.record_failure(_S(), "content_probe", "boom")
    for _ in range(vh.ALERT_THRESHOLD - 1):
        vh.record_failure(_S(), "cover_pick", "boom")
    assert sent == []
    vh.record_failure(_S(), "cover_pick", "boom")   # 只有 cover_pick 到阈值
    assert len(sent) == 1 and "cover_pick" in sent[0]
    # 探针仍差一次
    assert vh.snapshot()["content_probe"]["failures"] == vh.ALERT_THRESHOLD - 1


def test_reset_clears_and_notifies_recovery(sent):
    for _ in range(vh.ALERT_THRESHOLD):
        vh.record_failure(_S(), "content_probe", "boom")
    assert len(sent) == 1

    vh.reset("content_probe")
    assert vh.snapshot()["content_probe"] == {"failures": 0, "alerted": False}
    assert len(sent) == 2 and "恢复" in sent[1]


def test_reset_without_prior_alert_is_silent(sent):
    """没告警过就不发恢复通知（每次成功都发会变成噪音）。"""
    vh.record_failure(_S(), "content_probe", "boom")
    vh.reset("content_probe")
    assert sent == []


def test_can_re_alert_after_recovery(sent):
    """恢复后再次恶化要能重新告警（否则第二次故障就哑了）。"""
    for _ in range(vh.ALERT_THRESHOLD):
        vh.record_failure(_S(), "content_probe", "boom")
    vh.reset("content_probe")
    for _ in range(vh.ALERT_THRESHOLD):
        vh.record_failure(_S(), "content_probe", "boom again")
    assert len(sent) == 3          # 告警 + 恢复 + 再告警
    assert "boom again" in sent[2]


def test_notify_failure_does_not_raise(monkeypatch):
    """TG 推送失败不能把流水线带崩。

    直接用模块里**未被 fixture 替换**的真实 _notify 实现：从源码重新取一次，
    确保验证的是生产代码的 try/except，而不是测试桩。
    """
    import importlib

    import bot.ai.vision_health as mod
    from bot.notify import telegram

    monkeypatch.setattr(telegram, "notify_info",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    fresh = importlib.reload(mod)          # 拿回真实实现（reload 会重置 _notify）
    fresh._notify("测试告警")              # 不抛即通过
    assert fresh is not None


# ---------- 接线：三处调用点必须真的上报 ----------

def test_vision_modules_report_failures():
    """结构约束：三个视觉模块都要接上哨兵，否则又是静默降级。"""
    import inspect

    from bot.ai import content_probe, cover_pick, vision

    for mod, fn in ((content_probe, "_ask_vision"),
                    (cover_pick, "_vision_rank"),
                    (vision, "inspect_cover")):
        src = inspect.getsource(getattr(mod, fn))
        assert "record_failure" in src, f"{mod.__name__}.{fn} 未上报失败"
        assert "reset" in src, f"{mod.__name__}.{fn} 未上报成功"

"""平台风控冷却状态机测试。

核心行为：
1. 风控信号 → 风险等级递增 → 冷却时间阶梯上升（30min→2h→6h→24h）
2. 登录失效 / 网络问题**不冷却**（否则一次抖动停发半小时）
3. 连续成功才降级（渐进恢复，不是一键归零）
4. 冷却状态持久化（重启不清零）
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bot import risk


def _state(platform="kuaishou", **kw) -> risk.RiskState:
    return risk.RiskState(platform=platform, **kw)


class TestClassifyFailure:
    @pytest.mark.parametrize("text", [
        "操作过于频繁",
        "访问频繁，请稍后再试",
        "安全验证",
        "请拖动滑块完成验证",
        "http 429",
        "rate limit exceeded",
        "verification_required",
    ])
    def test_risk_signals(self, text):
        category, signal = risk.classify_failure(text)
        assert category == "risk", f"{text} 应判为风控，实际 {category}"

    @pytest.mark.parametrize("text", [
        "登录态已失效，请重新扫码",
        "logged_out:快手创作平台未登录",
        "connection timeout",
        "ReadTimeout: timed out",
        "proxy error",
    ])
    def test_non_risk_signals(self, text):
        category, _ = risk.classify_failure(text)
        assert category in ("auth", "network"), f"{text} 不该判为风控"

    def test_auth_specifically(self):
        assert risk.classify_failure("登录态已失效")[0] == "auth"
        assert risk.classify_failure("logged_out")[0] == "auth"

    def test_network_specifically(self):
        assert risk.classify_failure("connection timeout")[0] == "network"
        assert risk.classify_failure("ReadTimeout")[0] == "network"

    def test_login_beats_soft_risk(self):
        """报错里同时含「登录」和「请稍后再试」时，必须判登录而不是风控。

        否则一次 session 过期会让平台被无故冷却 30 分钟。
        """
        category, _ = risk.classify_failure("登录态已失效，请稍后再试")
        assert category == "auth"

    def test_business_default(self):
        assert risk.classify_failure("找不到上传入口")[0] == "business"
        assert risk.classify_failure("")[0] == "business"


class TestCooldownLadder:
    def test_ladder_values(self):
        steps = risk.DEFAULT_COOLDOWN_STEPS
        assert steps == (1800, 7200, 21600, 86400)

    def test_level_to_cooldown(self):
        assert risk.cooldown_for_level(0) == 0
        assert risk.cooldown_for_level(1) == 1800
        assert risk.cooldown_for_level(2) == 7200
        assert risk.cooldown_for_level(3) == 21600
        assert risk.cooldown_for_level(4) == 86400

    def test_over_max_clamps_to_last(self):
        """超过 4 级仍取最后一档，不能越界。"""
        assert risk.cooldown_for_level(9) == 86400


class TestApplyFailure:
    def test_risk_escalates(self):
        st = _state()
        now = datetime(2026, 11, 9, 12, 0, 0)
        risk.apply_failure(st, "risk", "操作过于频繁", now=now)
        assert st.risk_level == 1
        assert st.cooldown_remaining_sec(now) == 1800

    def test_repeated_risk_escalates_ladder(self):
        st = _state()
        now = datetime(2026, 11, 9, 12, 0, 0)
        for expected_level, expected_cd in ((1, 1800), (2, 7200), (3, 21600), (4, 86400)):
            risk.apply_failure(st, "risk", "风控", now=now)
            assert st.risk_level == expected_level
            assert st.cooldown_remaining_sec(now) == expected_cd

    def test_risk_level_capped(self):
        st = _state()
        now = datetime(2026, 11, 9, 12, 0, 0)
        for _ in range(10):
            risk.apply_failure(st, "risk", "风控", now=now)
        assert st.risk_level == risk.MAX_RISK_LEVEL

    @pytest.mark.parametrize("category", ["auth", "network", "business"])
    def test_non_risk_does_not_cool(self, category):
        """非风控失败绝不能设置冷却。"""
        st = _state()
        now = datetime(2026, 11, 9, 12, 0, 0)
        risk.apply_failure(st, category, "something", now=now)
        assert st.risk_level == 0
        assert st.cooldown_until == ""

    def test_risk_records_reason(self):
        st = _state()
        risk.apply_failure(st, "risk", "risk:操作过于频繁")
        assert "操作过于频繁" in st.last_risk_reason


class TestApplySuccess:
    def test_noop_when_healthy(self):
        st = _state(risk_level=0)
        risk.apply_success(st)
        assert st.risk_level == 0

    def test_gradual_recovery_not_immediate(self):
        """核心行为：一次成功不能把等级清零（否则阶梯形同虚设）。"""
        st = _state(risk_level=2)
        risk.apply_success(st)
        assert st.risk_level == 2, "单次成功不应降级"
        assert st.recovery_successes == 1

    def test_recovery_after_n_successes(self):
        st = _state(risk_level=2)
        for _ in range(3):
            risk.apply_success(st)
        assert st.risk_level == 1
        assert st.recovery_successes == 0

    def test_reaches_zero_and_clears_cooldown(self):
        st = _state(risk_level=1)
        risk.apply_success(st, recovery_successes=1)
        assert st.risk_level == 0
        assert st.cooldown_until == ""

    def test_success_resets_failure_counter(self):
        st = _state(consecutive_failures=5)
        risk.apply_success(st)
        assert st.consecutive_failures == 0


class TestGate:
    def test_allows_when_no_state(self):
        assert risk.evaluate_publish_gate(None).allowed is True

    def test_allows_when_healthy(self):
        st = _state(risk_level=0)
        assert risk.evaluate_publish_gate(st).allowed is True

    def test_blocks_during_cooldown(self):
        now = datetime(2026, 11, 9, 12, 0, 0)
        st = _state(risk_level=2,
                    cooldown_until=(now + timedelta(hours=2)).isoformat())
        decision = risk.evaluate_publish_gate(st, now=now)
        assert decision.allowed is False
        assert "冷却期" in decision.reason
        assert decision.retry_after_sec == 7200

    def test_allows_after_cooldown_expired(self):
        now = datetime(2026, 11, 9, 12, 0, 0)
        st = _state(risk_level=2,
                    cooldown_until=(now - timedelta(minutes=1)).isoformat())
        assert risk.evaluate_publish_gate(st, now=now).allowed is True


class TestPersistence:
    def test_roundtrip(self, tmp_path, monkeypatch):
        """状态必须能落库再读回，否则重启就绕过冷却。"""
        from bot import db as db_mod
        from bot.risk import RiskState

        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "t.db")
        d = db_mod.Database()
        st = RiskState(platform="kuaishou", risk_level=3,
                       cooldown_until="2026-11-09T20:00:00",
                       last_risk_reason="操作过于频繁")
        d.save_risk_state(st)

        loaded = d.get_risk_state("kuaishou")
        assert loaded is not None
        assert loaded.risk_level == 3
        assert loaded.cooldown_until == "2026-11-09T20:00:00"
        assert loaded.last_risk_reason == "操作过于频繁"

    def test_upsert_updates_existing(self, tmp_path, monkeypatch):
        from bot import db as db_mod
        from bot.risk import RiskState

        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "t.db")
        d = db_mod.Database()
        d.save_risk_state(RiskState(platform="kuaishou", risk_level=1))
        d.save_risk_state(RiskState(platform="kuaishou", risk_level=3))
        assert d.get_risk_state("kuaishou").risk_level == 3

    def test_missing_returns_none(self, tmp_path, monkeypatch):
        from bot import db as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "t.db")
        d = db_mod.Database()
        assert d.get_risk_state("nothing") is None


class TestFormatDuration:
    def test_minutes(self):
        assert "分钟" in risk.format_duration(1800)

    def test_hours(self):
        assert "小时" in risk.format_duration(7200)

    def test_days(self):
        assert "天" in risk.format_duration(86400)

    def test_zero(self):
        assert risk.format_duration(0) == "0秒"


class TestManualClear:
    def test_clears_everything(self):
        st = _state(risk_level=4, cooldown_until="2026-11-10T00:00:00",
                    recovery_successes=2, consecutive_failures=5,
                    last_risk_reason="频繁")
        risk.manual_clear(st)
        assert st.risk_level == 0
        assert st.cooldown_until == ""
        assert st.recovery_successes == 0
        assert st.consecutive_failures == 0
        assert st.last_risk_reason == ""

"""账号环境隔离诊断测试。

这个模块回答的问题是：多个账号会不会被平台判定成「同一操作者」。
判断错误的后果是两个方向：
  - 漏报（该报警没报）→ 用户以为安全，实际一个号连坐全部；
  - 误报（把独立的账号判成关联）→ 用户白折腾去改配置。

所以两侧都要锁。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot import audit_env as ae


def _env(platform, fp="", ip="", exists=True):
    e = ae.AccountEnv(
        platform=platform,
        profile_path=f"/tmp/{platform}.json",
        profile_exists=exists,
        credential_fp=fp,
        exit_ip=ip,
    )
    e.environment_id = e.fingerprint()
    return e


class TestFingerprint:
    def test_same_inputs_same_fingerprint(self):
        a = _env("kuaishou", fp="AAA", ip="1.1.1.1")
        b = _env("kuaishou", fp="AAA", ip="1.1.1.1")
        assert a.environment_id == b.environment_id

    def test_different_platform_differs(self):
        a = _env("kuaishou", fp="AAA", ip="1.1.1.1")
        b = _env("kuaishou2", fp="AAA", ip="1.1.1.1")
        assert a.environment_id != b.environment_id

    def test_different_credential_differs(self):
        a = _env("kuaishou", fp="AAA", ip="1.1.1.1")
        b = _env("kuaishou", fp="BBB", ip="1.1.1.1")
        assert a.environment_id != b.environment_id

    def test_different_ip_differs(self):
        a = _env("kuaishou", fp="AAA", ip="1.1.1.1")
        b = _env("kuaishou", fp="AAA", ip="2.2.2.2")
        assert a.environment_id != b.environment_id


class TestSameNetworkGroups:
    def test_detects_shared_ip(self):
        envs = [_env("a", ip="1.1.1.1"), _env("b", ip="1.1.1.1"), _env("c", ip="2.2.2.2")]
        rep = ae.IsolationReport(accounts=envs)
        groups = rep.same_network_groups()
        assert "1.1.1.1" in groups
        assert set(groups["1.1.1.1"]) == {"a", "b"}
        assert "2.2.2.2" not in groups, "单账号 IP 不该进分组"

    def test_empty_ip_ignored(self):
        envs = [_env("a", ip=""), _env("b", ip="")]
        rep = ae.IsolationReport(accounts=envs)
        assert rep.same_network_groups() == {}


class TestDuplicateCredentials:
    def test_detects_reused_login_state(self):
        """两个账号共用同一份登录态是最危险配置，必须报出来。"""
        envs = [_env("kuaishou", fp="SHARED"), _env("kuaishou2", fp="SHARED")]
        rep = ae.IsolationReport(accounts=envs)
        dup = rep.duplicate_credentials()
        assert len(dup) == 1
        assert set(list(dup.values())[0]) == {"kuaishou", "kuaishou2"}

    def test_unique_credentials_no_alert(self):
        envs = [_env("kuaishou", fp="AAA"), _env("kuaishou2", fp="BBB")]
        rep = ae.IsolationReport(accounts=envs)
        assert rep.duplicate_credentials() == {}

    def test_empty_fingerprint_ignored(self):
        envs = [_env("a", fp=""), _env("b", fp="")]
        rep = ae.IsolationReport(accounts=envs)
        assert rep.duplicate_credentials() == {}


class TestAnalyze:
    def test_reports_shared_ip(self):
        envs = [_env("a", fp="X", ip="1.1.1.1"), _env("b", fp="Y", ip="1.1.1.1")]
        rep = ae.analyze(envs)
        assert any("共享出口 IP" in w for w in rep.warnings)

    def test_reports_duplicate_credentials_as_critical(self):
        envs = [_env("a", fp="SAME", ip="1.1.1.1"), _env("b", fp="SAME", ip="1.1.1.1")]
        rep = ae.analyze(envs)
        assert any("同一份登录态" in w for w in rep.warnings)

    def test_reports_missing_state(self):
        envs = [_env("a", fp="", ip="1.1.1.1", exists=False)]
        rep = ae.analyze(envs)
        assert any("缺少登录态" in w for w in rep.warnings)

    def test_clean_setup_no_warnings(self):
        envs = [
            _env("kuaishou", fp="AAA", ip="1.1.1.1"),
            _env("kuaishou2", fp="BBB", ip="2.2.2.2"),
        ]
        rep = ae.analyze(envs)
        assert rep.warnings == [], f"独立环境不该有告警：{rep.warnings}"

    def test_ig_shared_ip_is_info_not_error(self):
        """IG 走官方 API，同 IP 不是封号主因，只提示。"""
        envs = [
            _env("ig_pets", fp="A", ip="9.9.9.9"),
            _env("ig_anim", fp="B", ip="9.9.9.9"),
        ]
        rep = ae.analyze(envs)
        ig_notes = [w for w in rep.warnings if w.startswith("ℹ️")]
        assert ig_notes, "应有一条 IG 专属提示"


class TestCredentialFingerprint:
    def test_same_cookies_different_format_same_fp(self, tmp_path):
        """storage_state 的键顺序/空白不应影响指纹（否则漏报重复登录态）。"""
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        a.write_text(json.dumps({
            "cookies": [{"name": "userId", "value": "123", "domain": ".kuaishou.com"}]
        }), encoding="utf-8")
        b.write_text(json.dumps({
            "cookies": [{"domain": ".kuaishou.com", "value": "123", "name": "userId"}]
        }, indent=2), encoding="utf-8")

        assert ae._fp_storage_state(a) == ae._fp_storage_state(b)
        assert ae._fp_storage_state(a) != ""

    def test_different_cookie_value_differs(self, tmp_path):
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        a.write_text(json.dumps({"cookies": [{"name": "sid", "value": "AAA"}]}))
        b.write_text(json.dumps({"cookies": [{"name": "sid", "value": "BBB"}]}))
        assert ae._fp_storage_state(a) != ae._fp_storage_state(b)

    def test_missing_file_returns_empty(self, tmp_path):
        assert ae._fp_storage_state(tmp_path / "nope.json") == ""

    def test_token_fp_hides_plaintext(self):
        fp = ae._fp_token("EAAP3CsecretTOKENvalue")
        assert "secret" not in fp
        assert len(fp) == 16


class TestCollectEnvs:
    def test_collects_browser_platforms(self, tmp_path):
        state = tmp_path / "ks_state.json"
        state.write_text(json.dumps({"cookies": [{"name": "userId", "value": "1"}]}))
        envs = ae.collect_envs(tmp_path, platforms=None, probe_ip=False)
        ks = [e for e in envs if e.platform == "kuaishou"]
        assert ks and ks[0].profile_exists is True
        assert ks[0].credential_fp != ""

    def test_skips_disabled_platforms(self, tmp_path):
        from bot.config import PlatformConfig
        platforms = {"kuaishou": PlatformConfig(enabled=False)}
        envs = ae.collect_envs(tmp_path, platforms=platforms, probe_ip=False)
        assert not any(e.platform == "kuaishou" for e in envs)

    def test_includes_ig_accounts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IG_TOKEN_IG_PETS", "tok1")
        monkeypatch.setenv("IG_USERID_IG_PETS", "uid1")
        monkeypatch.delenv("IG_TOKEN_IG_ANIM", raising=False)
        envs = ae.collect_envs(tmp_path, platforms=None, probe_ip=False)
        pets = [e for e in envs if e.platform == "ig_pets"]
        assert pets and pets[0].profile_exists is True
        assert pets[0].credential_fp != ""

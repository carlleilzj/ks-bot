"""回归测试：Remote API 安全与热榜归档加固（2026-11-04 深度审计修复）。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from bot.remote_api import RemoteApi, RemoteApiError


def _api():
    return RemoteApi(MagicMock(), MagicMock(), lambda s, db, p: None)


# ---------- 绑定地址白名单 ----------

@pytest.mark.parametrize("host", ["0.0.0.0", "::", "8.8.8.8", "1.1.1.1"])
def test_serve_forever_rejects_public_or_unspecified_bind(host):
    """0.0.0.0 / :: 在 Python 里 is_private=True，旧校验会放行 → 暴露公网。"""
    api = _api()
    with pytest.raises(RemoteApiError):
        api.serve_forever(host, 0)


def test_serve_forever_rejects_non_ip_bind():
    api = _api()
    with pytest.raises(RemoteApiError):
        api.serve_forever("localhost", 0)


# ---------- 热榜归档校验 ----------

def test_hot_topics_rejects_unknown_platform(tmp_path, monkeypatch):
    """别的平台榜单不能覆盖快手榜。"""
    f = tmp_path / "k.json"
    monkeypatch.setattr("bot.monitor.kuaishou_hot.KS_HOT_FILE", f)
    res = _api().hot_topics_ingest({"platform": "douyin",
                                   "topics": [{"topic": "抖音热点"}]})
    assert res["ok"] is False
    assert not f.exists()


def test_hot_topics_sanitizes_entries(tmp_path, monkeypatch):
    """脏数据必须被清洗：非 dict、空 topic、超长 topic、超量条数。"""
    f = tmp_path / "k.json"
    monkeypatch.setattr("bot.monitor.kuaishou_hot.KS_HOT_FILE", f)

    topics = [{"topic": "正常热点", "hot_value": "100万"},
              "不是字典",
              {"topic": "   ", "hot_value": "1"},
              {"topic": "长" * 80, "hot_value": "9" * 40}]
    topics += [{"topic": f"溢出{i}"} for i in range(200)]

    res = _api().hot_topics_ingest({"platform": "kuaishou", "topics": topics})
    assert res["ok"] is True

    saved = json.loads(f.read_text(encoding="utf-8"))
    assert saved["count"] <= 100
    assert len(saved["topics"]) <= 100
    assert all(isinstance(t["topic"], str) and t["topic"] for t in saved["topics"])
    assert all(len(t["topic"]) <= 32 for t in saved["topics"])
    assert saved["topics"][0]["topic"] == "正常热点"
    # rank 重新连续编号
    assert [t["rank"] for t in saved["topics"][:3]] == [1, 2, 3]


def test_hot_topics_rejects_all_invalid(tmp_path, monkeypatch):
    f = tmp_path / "k.json"
    monkeypatch.setattr("bot.monitor.kuaishou_hot.KS_HOT_FILE", f)
    res = _api().hot_topics_ingest({"platform": "kuaishou", "topics": [{"topic": ""}]})
    assert res["ok"] is False
    assert not f.exists()


def test_hot_topics_write_is_atomic_no_tmp_left(tmp_path, monkeypatch):
    f = tmp_path / "k.json"
    monkeypatch.setattr("bot.monitor.kuaishou_hot.KS_HOT_FILE", f)
    _api().hot_topics_ingest({"platform": "kuaishou",
                              "topics": [{"topic": "热点A"}]})
    assert f.exists()
    assert not f.with_suffix(".json.tmp").exists()

"""测试快手热门话题抓取、归档与 Prompt 注入。"""

import json
from unittest.mock import MagicMock, patch

from bot.monitor.kuaishou_hot import (
    fetch_kuaishou_hot_topics,
    format_hot_topics_for_prompt,
    load_cached_hot_topics,
)


def test_format_hot_topics_for_prompt():
    topics = [
        {"rank": 1, "topic": "神龙大侠", "hot_value": "1200万"},
        {"rank": 2, "topic": "搞笑动画", "hot_value": "900万"},
    ]
    txt = format_hot_topics_for_prompt(topics)
    assert "1. 神龙大侠 (热度 1200万)" in txt
    assert "2. 搞笑动画 (热度 900万)" in txt
    assert format_hot_topics_for_prompt([]) == ""


def test_load_cached_hot_topics(tmp_path, monkeypatch):
    mock_file = tmp_path / "kuaishou_hot_topics.json"
    monkeypatch.setattr("bot.monitor.kuaishou_hot.KS_HOT_FILE", mock_file)

    # 不存在时返回空列表
    assert load_cached_hot_topics() == []

    # 写入测试数据
    data = {
        "platform": "kuaishou",
        "updated_at": "2026-11-03T08:00:00",
        "count": 2,
        "topics": [{"rank": 1, "topic": "测试热点", "hot_value": "500万"}],
    }
    mock_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    loaded = load_cached_hot_topics(max_age_hours=100000.0)
    assert len(loaded) == 1
    assert loaded[0]["topic"] == "测试热点"


def test_fetch_kuaishou_hot_topics_parse():
    mock_html = """
    <html>
    <script>window.__APOLLO_STATE__={"defaultClient":{"ROOT_QUERY":{"visionHotRank({\\"page\\":\\"home\\"})":{"items":[{"id":"VisionHotRankItem:热搜1"},{"id":"VisionHotRankItem:置顶通知"}]}},"VisionHotRankItem:热搜1":{"name":"热搜1","hotValue":"1000万","tagType":null},"VisionHotRankItem:置顶通知":{"name":"置顶通知","hotValue":"2000万","tagType":"置顶"}}};(function(){})()</script>
    </html>
    """
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = mock_html
    mock_client.get.return_value = mock_resp
    mock_client.__enter__.return_value = mock_client

    with patch("httpx.Client", return_value=mock_client):
        res = fetch_kuaishou_hot_topics()
        assert len(res) == 1
        assert res[0]["topic"] == "热搜1"
        assert res[0]["hot_value"] == "1000万"
        assert res[0]["rank"] == 1


def test_kuaishou_profile_tags_role():
    from bot.ai.copywriter import PLATFORM_PROFILES, _system_prompt

    prof = PLATFORM_PROFILES["kuaishou"]
    assert prof.tag_base == "", "快手 A 号不设固定锚点"
    assert prof.tag_content_slots == 4, "4 槽位全给内容"

    prompt = _system_prompt(prof)
    assert "严格生成 4 个话题" in prompt
    assert "行业垂直赛道词" in prompt
    assert "画面场景/角色主体词" in prompt
    assert "情绪动机/互动钩子词" in prompt
    assert "平台热门/破圈标签" in prompt


def test_remote_api_hot_topics_ingest(tmp_path, monkeypatch):
    from bot.remote_api import RemoteApi

    mock_file = tmp_path / "kuaishou_hot_topics.json"
    monkeypatch.setattr("bot.monitor.kuaishou_hot.KS_HOT_FILE", mock_file)

    api = RemoteApi(MagicMock(), MagicMock(), lambda s, db, p: None)
    res = api.hot_topics_ingest({
        "platform": "kuaishou",
        "topics": [{"rank": 1, "topic": "测试热点1", "hot_value": "100万"}],
    })
    assert res["ok"] is True
    assert res["count"] == 1
    assert mock_file.exists()
    content = json.loads(mock_file.read_text(encoding="utf-8"))
    assert content["topics"][0]["topic"] == "测试热点1"

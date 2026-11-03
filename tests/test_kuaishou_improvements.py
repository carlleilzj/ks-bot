"""测试快手平台优化项（借鉴 MatrixMedia 实战配方）：
1. 描述框防静默漏发与提交前断言（_assert_desc_not_empty）
2. 作品管理页 clientCacheKey 逆推视频正式短链接（_fetch_ks_url）
"""
from unittest.mock import MagicMock
import pytest

from bot.publish.kuaishou import (
    _read_desc_text,
    _assert_desc_not_empty,
    _fetch_ks_url,
    KuaishouError,
)


def test_read_desc_text():
    mock_page = MagicMock()
    mock_page.evaluate.return_value = "快手标题 #搞笑"
    assert _read_desc_text(mock_page) == "快手标题 #搞笑"


def test_assert_desc_not_empty_success():
    mock_page = MagicMock()
    mock_page.evaluate.return_value = "已有描述内容"
    # 不抛异常即为通过
    _assert_desc_not_empty(mock_page, "已有描述内容")


def test_assert_desc_not_empty_aborts_when_blank():
    mock_page = MagicMock()
    # 模拟描述框为空，且重试注入后仍然为空
    mock_page.evaluate.return_value = ""

    with pytest.raises(KuaishouError) as exc_info:
        _assert_desc_not_empty(mock_page, "预期的描述内容")

    assert "快手提交前校验失败" in str(exc_info.value)
    assert "描述编辑器为空" in str(exc_info.value)


def test_fetch_ks_url_via_client_cache_key():
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_context.new_page.return_value = mock_page

    # 模拟 evaluate 脚本成功通过 clientCacheKey 找到作品
    mock_page.evaluate.return_value = {
        "url": "https://www.kuaishou.com/short-video/3xabc12345",
        "title": "测试作品标题",
        "idx": 2,
    }

    url = _fetch_ks_url(mock_context, title="测试作品标题", max_attempts=1)
    assert url == "https://www.kuaishou.com/short-video/3xabc12345"
    mock_page.close.assert_called_once()


def test_fetch_ks_url_returns_none_when_unmatched():
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_context.new_page.return_value = mock_page

    # clientCacheKey 未匹配到任何非置顶有效作品
    mock_page.evaluate.return_value = None

    url = _fetch_ks_url(mock_context, title="不匹配的标题", max_attempts=1)
    assert url is None
    mock_page.close.assert_called_once()

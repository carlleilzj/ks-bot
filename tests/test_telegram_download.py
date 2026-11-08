"""TG 文件下载函数测试（getFile → 流式下载 → 落盘）。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot.notify import telegram


class _FakeStream:
    def __init__(self, chunks: list[bytes], status: int = 200):
        self._chunks = chunks
        self.status_code = status

    def iter_bytes(self, n):
        for c in self._chunks:
            yield c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _settings():
    s = MagicMock()
    s.telegram_bot_token = "tok"
    s.telegram_chat_id = "123"
    s.telegram_api_base = "https://api.telegram.org"
    s.telegram_proxy = ""
    return s


class TestGetFilePath:
    def test_returns_path_from_api(self):
        s = _settings()
        with patch.object(telegram, "_post",
                          return_value={"ok": True,
                                        "result": {"file_path": "videos/file_1.mp4"}}):
            assert telegram.get_file_path(s, "fid") == "videos/file_1.mp4"

    def test_returns_none_on_api_failure(self):
        s = _settings()
        with patch.object(telegram, "_post", return_value=None):
            assert telegram.get_file_path(s, "fid") is None

    def test_returns_none_when_no_path_key(self):
        s = _settings()
        with patch.object(telegram, "_post", return_value={"ok": True, "result": {}}):
            assert telegram.get_file_path(s, "fid") is None


class TestDownloadFile:
    def test_downloads_and_writes_file(self, tmp_path):
        s = _settings()
        dest = tmp_path / "out.mp4"
        chunks = [b"a" * 1000, b"b" * 1000]

        with patch.object(telegram, "get_file_path", return_value="videos/x.mp4"), \
             patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.stream.return_value = _FakeStream(chunks)

            result = telegram.download_file(s, "fid", dest)

        assert result == dest
        assert dest.exists()
        assert dest.read_bytes() == b"a" * 1000 + b"b" * 1000
        # 中间文件不能残留
        assert not dest.with_suffix(dest.suffix + ".part").exists()

    def test_returns_none_when_getfile_fails(self, tmp_path):
        s = _settings()
        dest = tmp_path / "out.mp4"
        with patch.object(telegram, "get_file_path", return_value=None):
            assert telegram.download_file(s, "fid", dest) is None
        assert not dest.exists()

    def test_returns_none_on_http_error(self, tmp_path):
        s = _settings()
        dest = tmp_path / "out.mp4"
        with patch.object(telegram, "get_file_path", return_value="v.mp4"), \
             patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.stream.return_value = _FakeStream([], status=404)
            assert telegram.download_file(s, "fid", dest) is None
        assert not dest.exists()

    def test_cleans_partial_file_on_exception(self, tmp_path):
        """下载中断必须清掉 .part，否则下次会误判「已下载」。"""
        s = _settings()
        dest = tmp_path / "out.mp4"
        with patch.object(telegram, "get_file_path", return_value="v.mp4"), \
             patch("httpx.Client", side_effect=RuntimeError("network died")):
            assert telegram.download_file(s, "fid", dest) is None
        assert not dest.exists()
        assert not dest.with_suffix(dest.suffix + ".part").exists()

    def test_creates_parent_dirs(self, tmp_path):
        s = _settings()
        dest = tmp_path / "deep" / "nested" / "out.mp4"
        with patch.object(telegram, "get_file_path", return_value="v.mp4"), \
             patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.stream.return_value = _FakeStream([b"data"])
            assert telegram.download_file(s, "fid", dest) == dest
        assert dest.exists()

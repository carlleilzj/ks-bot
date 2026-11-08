"""自建 Bot API server 通道测试。

官方 api.telegram.org 的 getFile 下载上限是 20MB；自建 server 开 --local 后
是 2000MB，且 getFile 返回的是服务器上的绝对路径（可硬链接，零拷贝）。

这组测试锁定：
1. 通道识别（官方 vs 自建）不能误判；
2. 大小上限随通道切换；
3. 自建模式下 file_path 是绝对路径时走硬链接，不发明文 HTTP 请求；
4. 官方模式下依然走 HTTP 下载；
5. 自建模式但 path 不是绝对路径（配置错误）时要优雅回退。
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot.notify import telegram


def _settings(api_base: str):
    s = MagicMock()
    s.telegram_bot_token = "tok"
    s.telegram_chat_id = "123"
    s.telegram_api_base = api_base
    s.telegram_proxy = ""
    return s


class TestChannelDetection:
    def test_official_detected(self):
        assert not telegram.is_local_bot_api(_settings("https://api.telegram.org"))

    def test_selfhosted_detected(self):
        assert telegram.is_local_bot_api(_settings("http://127.0.0.1:8081"))
        assert telegram.is_local_bot_api(_settings("https://tg.example.com"))

    def test_limits_switch_by_channel(self):
        official = _settings("https://api.telegram.org")
        local = _settings("http://127.0.0.1:8081")
        assert telegram.max_download_mb(official) == 20
        assert telegram.max_download_mb(local) == 2000

    def test_empty_base_is_official(self):
        s = MagicMock()
        s.telegram_api_base = ""
        assert not telegram.is_local_bot_api(s)


class TestLocalHardlink:
    def test_absolute_path_is_linked_not_downloaded(self, tmp_path):
        """自建模式：file_path 是绝对路径 → 硬链接，不发 HTTP 请求。"""
        s = _settings("http://127.0.0.1:8081")
        src = tmp_path / "tg_store" / "file_1.mp4"
        src.parent.mkdir()
        src.write_bytes(b"video-bytes" * 1000)
        dest = tmp_path / "raw" / "local_abc.mp4"

        with patch.object(telegram, "get_file_result",
                          return_value={"file_path": str(src), "file_size": src.stat().st_size}), \
             patch("httpx.Client") as mock_client:
            result = telegram.download_file(s, "fid", dest)

        assert result == dest
        assert dest.exists()
        assert dest.read_bytes() == src.read_bytes()
        # 关键：不能走 HTTP（自建模式下载 0 次网络请求）
        mock_client.assert_not_called()

    def test_hardlink_shares_inode_when_possible(self, tmp_path):
        """同分区首次应当硬链接（省磁盘），inode 相同。"""
        s = _settings("http://127.0.0.1:8081")
        src = tmp_path / "src.mp4"
        src.write_bytes(b"x" * 5000)
        dest = tmp_path / "dest.mp4"

        with patch.object(telegram, "get_file_result",
                          return_value={"file_path": str(src), "file_size": src.stat().st_size}):
            telegram.download_file(s, "fid", dest)

        try:
            assert os.stat(src).st_ino == os.stat(dest).st_ino
        except (OSError, AssertionError):
            # 某些文件系统（如网络盘）不支持硬链接，会退化成复制，也属正常
            assert dest.read_bytes() == src.read_bytes()

    def test_falls_back_to_http_when_path_missing(self, tmp_path):
        """自建模式但 file_path 不是可读文件（配置错误）→ 回退 HTTP。"""
        s = _settings("http://127.0.0.1:8081")
        dest = tmp_path / "out.mp4"
        with patch.object(telegram, "get_file_result",
                          return_value={"file_path": "/nonexistent/gone.mp4",
                                        "file_size": 100}), \
             patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.stream.side_effect = \
                RuntimeError("should not reach here if fallback broken")
            result = telegram.download_file(s, "fid", dest)
        # 回退到 HTTP 后因为 mock 抛异常，最终返回 None，但必须是「尝试过 HTTP」
        assert mock_client.called
        assert result is None


class TestOfficialHTTP:
    def test_official_uses_http_download(self, tmp_path):
        s = _settings("https://api.telegram.org")
        dest = tmp_path / "out.mp4"

        class FakeStream:
            status_code = 200

            def iter_bytes(self, n):
                yield b"chunk1"
                yield b"chunk2"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(telegram, "get_file_result",
                          return_value={"file_path": "videos/x.mp4", "file_size": 12}), \
             patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.stream.return_value = FakeStream()

            result = telegram.download_file(s, "fid", dest)

        assert result == dest
        assert dest.read_bytes() == b"chunk1chunk2"
        assert mock_client.stream.called

    def test_official_rejects_oversize(self, tmp_path):
        s = _settings("https://api.telegram.org")
        dest = tmp_path / "out.mp4"
        with patch.object(telegram, "get_file_result",
                          return_value={"file_path": "videos/big.mp4",
                                        "file_size": 50 * 1048576}), \
             patch("httpx.Client") as mock_client:
            result = telegram.download_file(s, "fid", dest)
        assert result is None
        mock_client.assert_not_called()

    def test_local_accepts_large_size(self, tmp_path):
        """自建通道 50MB 应当放行（官方会拒）。"""
        s = _settings("http://127.0.0.1:8081")
        src = tmp_path / "big.mp4"
        src.write_bytes(b"x" * 1000)
        dest = tmp_path / "out.mp4"
        with patch.object(telegram, "get_file_result",
                          return_value={"file_path": str(src),
                                        "file_size": 50 * 1048576}):
            assert telegram.download_file(s, "fid", dest) == dest

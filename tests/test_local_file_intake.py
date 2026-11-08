"""本地文件投链测试。

背景（2026-11-09）：Threads 等平台 yt-dlp 不支持、也没有公开 API，
用户只能手动下载。为此加了「直接发视频文件给 bot」的入口。

这组测试锁定几个「错了会污染队列」的点：
1. 非视频文件（图片/文档）必须拒绝，不能入库；
2. 超过 Bot API 下载上限要提前拦截并给出可操作建议；
3. 入库状态必须是 DOWNLOADED（跳过下载阶段），否则会去 yt-dlp 解析 local:// 而失败；
4. 同一文件重复发送不能重复入库；
5. 平台标记要从 caption 里解析（文件名也可能带）。
"""
from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest

from bot.db import State
from bot.source.telegram_listener import TelegramListener


def _listener():
    s = MagicMock()
    s.telegram_chat_id = "123"
    # 平台配置要真实：_split_disabled_targets 会按 enabled 过滤，
    # 用 MagicMock 会让所有平台被判定为「已停用」而静默丢弃。
    s.platforms = {
        "kuaishou": MagicMock(enabled=True),
        "ig_pets": MagicMock(enabled=True),
    }
    lst = TelegramListener.__new__(TelegramListener)
    lst.s = s
    lst.db = MagicMock()
    lst._wakeup = MagicMock()
    lst._handle_url = MagicMock()
    return lst


def _video_msg(file_id="fid1", unique_id="uniq1", size=5_000_000,
               mime="video/mp4", caption="", file_name=""):
    video = {"file_id": file_id, "file_unique_id": unique_id,
             "file_size": size, "mime_type": mime}
    if file_name:
        video["file_name"] = file_name
    msg = {"chat": {"id": "123"}, "video": video,
           "from": {"username": "tester"}}
    if caption:
        msg["caption"] = caption
    return msg


@pytest.fixture
def patched_tg():
    """只 patch 具体函数，保留 TG_MAX_DOWNLOAD_MB 等真实常量。"""
    with patch("bot.notify.telegram.send_text") as send_text, \
         patch("bot.notify.telegram.download_file") as download_file:
        yield send_text, download_file


def _sent_text(send_text) -> str:
    return send_text.call_args[0][1]


class TestFileTypeFilter:
    def test_rejects_image(self, patched_tg):
        send_text, download_file = patched_tg
        lst = _listener()
        doc = {"file_id": "f", "mime_type": "image/jpeg", "file_name": "x.jpg"}
        lst._handle_local_file({"chat": {"id": "123"}, "document": doc}, None, doc)
        assert "只接受视频" in _sent_text(send_text)
        assert not download_file.called
        lst.db.insert_video.assert_not_called()

    def test_accepts_video_mime(self, patched_tg, tmp_path):
        send_text, download_file = patched_tg
        lst = _listener()
        lst.db.find_by_source_url.return_value = None
        lst.db.insert_video.return_value = 7
        msg = _video_msg()
        download_file.return_value = tmp_path / "x.mp4"
        with patch("bot.source.telegram_listener.RAW_DIR", tmp_path), \
             patch("bot.media.ffmpeg.video_info",
                  return_value={"width": 720, "height": 1280, "duration": 12.0}):
            lst._handle_local_file(msg, msg["video"], {})
        lst.db.insert_video.assert_called_once()


class TestSizeLimit:
    def test_oversize_rejected_with_guidance(self, patched_tg):
        send_text, download_file = patched_tg
        lst = _listener()
        msg = _video_msg(size=200 * 1048576)
        lst._handle_local_file(msg, msg["video"], {})
        text = _sent_text(send_text)
        assert "超过" in text
        assert "20" in text
        assert "压缩" in text and "链接" in text
        assert not download_file.called


class TestStateMachineEntry:
    def test_inserts_as_downloaded_to_skip_download_stage(self, patched_tg, tmp_path):
        """必须是 DOWNLOADED：DETECTED 会走 yt-dlp，而 local:// 解析不了。"""
        send_text, download_file = patched_tg
        lst = _listener()
        lst.db.find_by_source_url.return_value = None
        lst.db.insert_video.return_value = 9
        msg = _video_msg()
        download_file.return_value = tmp_path / "x.mp4"
        with patch("bot.source.telegram_listener.RAW_DIR", tmp_path), \
             patch("bot.media.ffmpeg.video_info",
                  return_value={"width": 720, "height": 1280, "duration": 30.0}):
            lst._handle_local_file(msg, msg["video"], {})

        kwargs = lst.db.insert_video.call_args.kwargs
        assert kwargs.get("state") == State.DOWNLOADED

    def test_shortcode_is_local_prefix(self, patched_tg, tmp_path):
        send_text, download_file = patched_tg
        lst = _listener()
        lst.db.find_by_source_url.return_value = None
        lst.db.insert_video.return_value = 9
        msg = _video_msg(unique_id="abc123")
        download_file.return_value = tmp_path / "x.mp4"
        with patch("bot.source.telegram_listener.RAW_DIR", tmp_path), \
             patch("bot.media.ffmpeg.video_info",
                  return_value={"width": 720, "height": 1280, "duration": 30.0}):
            lst._handle_local_file(msg, msg["video"], {})

        meta = lst.db.insert_video.call_args[0][0]
        assert meta.shortcode.startswith("local_")
        assert "abc123" in meta.shortcode
        assert meta.platform == "local"


class TestDedupe:
    def test_same_file_not_inserted_twice(self, patched_tg):
        send_text, download_file = patched_tg
        lst = _listener()
        lst.db.find_by_source_url.return_value = {"state": State.PUBLISHED,
                                                  "shortcode": "local_uniq1"}
        msg = _video_msg()
        lst._handle_local_file(msg, msg["video"], {})
        assert "已经投过了" in _sent_text(send_text)
        assert not download_file.called
        lst.db.insert_video.assert_not_called()


class TestPlatformParsing:
    def _run(self, msg, patched_tg, tmp_path):
        send_text, download_file = patched_tg
        lst = _listener()
        lst.db.find_by_source_url.return_value = None
        lst.db.insert_video.return_value = 11
        download_file.return_value = tmp_path / "x.mp4"
        with patch("bot.source.telegram_listener.RAW_DIR", tmp_path), \
             patch("bot.media.ffmpeg.video_info",
                  return_value={"width": 720, "height": 1280, "duration": 10.0}):
            lst._handle_local_file(msg, msg["video"], {})
        return lst

    def test_platform_from_caption(self, patched_tg, tmp_path):
        lst = self._run(_video_msg(caption="@ig萌宠"), patched_tg, tmp_path)
        assert lst.db.insert_video.call_args.kwargs.get("target_platforms") == ["ig_pets"]

    def test_platform_from_filename(self, patched_tg, tmp_path):
        lst = self._run(_video_msg(file_name="clip_@快手.mp4"), patched_tg, tmp_path)
        assert lst.db.insert_video.call_args.kwargs.get("target_platforms") == ["kuaishou"]

    def test_no_marker_uses_default_platforms(self, patched_tg, tmp_path):
        lst = self._run(_video_msg(), patched_tg, tmp_path)
        assert lst.db.insert_video.call_args.kwargs.get("target_platforms") is None


class TestValidation:
    def test_download_failure_reports_and_aborts(self, patched_tg):
        send_text, download_file = patched_tg
        lst = _listener()
        lst.db.find_by_source_url.return_value = None
        msg = _video_msg()
        download_file.return_value = None
        lst._handle_local_file(msg, msg["video"], {})
        assert "下载失败" in _sent_text(send_text)
        lst.db.insert_video.assert_not_called()

    def test_invalid_video_is_rejected(self, patched_tg, tmp_path):
        """伪装成 mp4 的文件不能进流水线。"""
        send_text, download_file = patched_tg
        lst = _listener()
        lst.db.find_by_source_url.return_value = None
        msg = _video_msg()
        fake = tmp_path / "local_x.mp4"
        fake.write_bytes(b"not a video")
        download_file.return_value = fake
        with patch("bot.source.telegram_listener.RAW_DIR", tmp_path), \
             patch("bot.media.ffmpeg.video_info",
                  side_effect=RuntimeError("not a video")):
            lst._handle_local_file(msg, msg["video"], {})
        assert "不是有效视频" in _sent_text(send_text)
        lst.db.insert_video.assert_not_called()

    def test_zero_duration_rejected(self, patched_tg, tmp_path):
        send_text, download_file = patched_tg
        lst = _listener()
        lst.db.find_by_source_url.return_value = None
        msg = _video_msg()
        fake = tmp_path / "zero.mp4"
        fake.write_bytes(b"x")
        download_file.return_value = fake
        with patch("bot.source.telegram_listener.RAW_DIR", tmp_path), \
             patch("bot.media.ffmpeg.video_info",
                  return_value={"width": 720, "height": 1280, "duration": 0}):
            lst._handle_local_file(msg, msg["video"], {})
        assert "时长为 0" in _sent_text(send_text)
        lst.db.insert_video.assert_not_called()

"""流水线集成测试：出海任务与国内任务的分流处理。

关键断言（错了会直接毁片）：
1. 出海任务的成品绝不能烧中文字幕；
2. 国内任务必须照常烧字幕（不能因为加了出海分支把国内搞坏）；
3. transcode 阶段对出海任务要调 export 清洗，对国内任务不调。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot.db import State


@pytest.fixture
def fake_task(tmp_path):
    def _make(targets: str | None):
        work = tmp_path / "work.mp4"
        work.write_bytes(b"x" * 20_000)
        raw = tmp_path / "raw.mp4"
        raw.write_bytes(b"y" * 20_000)
        return {
            "id": 1,
            "shortcode": "test_sc",
            "target_platforms": targets,
            "work_path": str(work),
            "raw_path": str(raw),
            "transcript": "",
        }
    return _make


class TestSubtitleBranch:
    def test_export_task_skips_chinese_subtitles(self, fake_task, tmp_path, monkeypatch):
        """出海任务绝不能把中文字幕烧进画面。"""
        from bot import main as bot_main

        task = fake_task("ig_pets")
        db = MagicMock()

        final_dir = tmp_path / "final"
        final_dir.mkdir()
        monkeypatch.setattr(bot_main, "FINAL_DIR", final_dir)
        monkeypatch.setattr(bot_main, "WORK_DIR", tmp_path)

        # 造一个「有字幕文件」的状态，验证即使有 ass 也不会烧
        ass = tmp_path / "test_sc.ass"
        ass.write_text("[Events]\n")

        with patch.object(bot_main.ffmpeg, "burn_subtitles") as mock_burn:
            bot_main.step_subtitle(MagicMock(), db, task)

        mock_burn.assert_not_called(), "出海任务竟然调用了字幕烧录！"
        db.update.assert_called_once()
        call_kwargs = db.update.call_args.kwargs
        assert call_kwargs["state"] == State.SUBTITLED
        assert (final_dir / "test_sc_final.mp4").exists()

    def test_domestic_task_still_burns_subtitles(self, fake_task, tmp_path, monkeypatch):
        """国内任务必须照常烧字幕——这条分支不能被出海改动带坏。"""
        from bot import main as bot_main

        task = fake_task("kuaishou")
        db = MagicMock()

        final_dir = tmp_path / "final"
        final_dir.mkdir()
        monkeypatch.setattr(bot_main, "FINAL_DIR", final_dir)
        monkeypatch.setattr(bot_main, "WORK_DIR", tmp_path)

        ass = tmp_path / "test_sc.ass"
        ass.write_text("[Events]\n")

        s = MagicMock()
        s.subtitle.enabled = True

        with patch.object(bot_main.ffmpeg, "burn_subtitles") as mock_burn:
            bot_main.step_subtitle(s, db, task)

        mock_burn.assert_called_once(), "国内任务的字幕烧录被误跳过了！"

    def test_domestic_task_without_ass_copies_file(self, fake_task, tmp_path, monkeypatch):
        from bot import main as bot_main

        task = fake_task("kuaishou")
        db = MagicMock()
        final_dir = tmp_path / "final"
        final_dir.mkdir()
        monkeypatch.setattr(bot_main, "FINAL_DIR", final_dir)
        monkeypatch.setattr(bot_main, "WORK_DIR", tmp_path)

        s = MagicMock()
        s.subtitle.enabled = True

        bot_main.step_subtitle(s, db, task)
        assert (final_dir / "test_sc_final.mp4").exists()


class TestTranscodeBranch:
    def test_export_task_runs_cleaning(self, fake_task, tmp_path, monkeypatch):
        """出海任务必须在转码阶段调 export 清洗（裁字幕区）。"""
        from bot import main as bot_main
        from bot.media import export as ex

        task = fake_task("ig_anim")
        db = MagicMock()
        monkeypatch.setattr(bot_main, "WORK_DIR", tmp_path)

        called = {}

        def fake_build(src, dst, prof, **kw):
            called["account"] = prof.name
            called["strip_audio"] = prof.strip_audio
            Path(dst).write_bytes(b"z" * 20_000)
            return {"audio": "stripped"}

        with patch.object(bot_main.ffmpeg, "ensure_compatible",
                          side_effect=lambda a, b: Path(b).write_bytes(b"a" * 20_000)), \
             patch.object(bot_main.ffmpeg, "strip_metadata"), \
             patch.object(bot_main.ffmpeg, "video_info",
                          return_value={"width": 1080, "height": 1920,
                                        "duration": 20, "has_audio": True,
                                        "vcodec": "h264", "acodec": "aac"}), \
             patch.object(bot_main.ffmpeg, "extract_cover"), \
             patch.object(ex, "build_export_video", side_effect=fake_build), \
             patch.object(ex, "verify_export", return_value=[]):
            bot_main.step_transcode(MagicMock(transform=MagicMock(enabled=False),
                                              watermark=MagicMock(enabled=False),
                                              vision=MagicMock(real_person_check=False)),
                                    db, task)

        assert called.get("account") == "ig_anim"
        assert called.get("strip_audio") is True

    def test_domestic_task_skips_cleaning(self, fake_task, tmp_path, monkeypatch):
        """国内任务绝不能走出海清洗（会裁掉正常画面）。"""
        from bot import main as bot_main
        from bot.media import export as ex

        task = fake_task("kuaishou")
        db = MagicMock()
        monkeypatch.setattr(bot_main, "WORK_DIR", tmp_path)

        with patch.object(bot_main.ffmpeg, "ensure_compatible",
                          side_effect=lambda a, b: Path(b).write_bytes(b"a" * 20_000)), \
             patch.object(bot_main.ffmpeg, "strip_metadata"), \
             patch.object(bot_main.ffmpeg, "video_info",
                          return_value={"width": 1080, "height": 1920,
                                        "duration": 20, "has_audio": True,
                                        "vcodec": "h264", "acodec": "aac"}), \
             patch.object(bot_main.ffmpeg, "extract_cover"), \
             patch.object(ex, "build_export_video") as mock_clean:
            bot_main.step_transcode(MagicMock(transform=MagicMock(enabled=False),
                                              watermark=MagicMock(enabled=False),
                                              vision=MagicMock(real_person_check=False)),
                                    db, task)

        mock_clean.assert_not_called(), "国内任务被误走了出海清洗！"


class TestExportCleaningFailure:
    def test_cleaning_failure_does_not_block_pipeline(self, fake_task, tmp_path, monkeypatch):
        """清洗失败要软降级（按原片继续），不能卡死整条流水线。"""
        from bot import main as bot_main
        from bot.media import export as ex

        task = fake_task("ig_pets")
        db = MagicMock()
        monkeypatch.setattr(bot_main, "WORK_DIR", tmp_path)

        with patch.object(bot_main.ffmpeg, "ensure_compatible",
                          side_effect=lambda a, b: Path(b).write_bytes(b"a" * 20_000)), \
             patch.object(bot_main.ffmpeg, "strip_metadata"), \
             patch.object(bot_main.ffmpeg, "extract_cover"), \
             patch.object(ex, "build_export_video",
                          side_effect=RuntimeError("ffmpeg 炸了")):
            bot_main.step_transcode(MagicMock(transform=MagicMock(enabled=False),
                                              watermark=MagicMock(enabled=False),
                                              vision=MagicMock(real_person_check=False)),
                                    db, task)

        # 仍然要推进到 TRANSCODED
        db.update.assert_called_once()
        assert db.update.call_args.kwargs["state"] == State.TRANSCODED

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot.publish import youtube as yt
from bot.publish.base import LoginExpired


class TestYouTubePublish:
    def test_load_account_missing_returns_none(self, monkeypatch):
        monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
        monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN", raising=False)
        assert yt.load_account() is None

    def test_load_account_success(self, monkeypatch):
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "my_cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "my_secret")
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "my_refresh")
        acct = yt.load_account()
        assert acct is not None
        assert acct.client_id == "my_cid"
        assert acct.client_secret == "my_secret"
        assert acct.refresh_token == "my_refresh"
        assert acct.is_configured is True

    def test_get_access_token_success(self, monkeypatch):
        acct = yt.YouTubeAccount("cid", "sec", "ref")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "ya29.test_token"}

        with patch("httpx.Client.post", return_value=mock_resp):
            tok = yt.get_access_token(acct)
            assert tok == "ya29.test_token"

    def test_get_access_token_expired_raises_login_expired(self, monkeypatch):
        acct = yt.YouTubeAccount("cid", "sec", "ref")

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "invalid_grant"

        with patch("httpx.Client.post", return_value=mock_resp):
            with pytest.raises(LoginExpired):
                yt.get_access_token(acct)

    def test_compose_description_adds_shorts_tag(self):
        desc = yt.compose_description(
            title="Mind-bending AI Short",
            description="A journey into the unknown galaxy.",
            tags=["SciFi", "CGI"],
        )
        assert "A journey into the unknown galaxy." in desc
        assert "#Shorts" in desc
        assert "#SciFi" in desc
        assert "#CGI" in desc

    def test_upload_video_resumable_flow(self, tmp_path, monkeypatch):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"dummy video content")

        acct = yt.YouTubeAccount("cid", "sec", "ref")
        monkeypatch.setattr(yt, "get_access_token", lambda a: "mock_tok")

        init_resp = MagicMock()
        init_resp.status_code = 200
        init_resp.headers = {"Location": "https://upload.example.com/resumable/123"}

        upload_resp = MagicMock()
        upload_resp.status_code = 200
        upload_resp.json.return_value = {"id": "yt_vid_abc"}

        with patch("httpx.Client.post", return_value=init_resp), \
             patch("httpx.Client.put", return_value=upload_resp):
            vid_id = yt.upload_video(
                acct=acct,
                video_path=video,
                title="Amazing AI Sci-Fi",
                description="Short story",
                tags=["AI", "SciFi"],
            )
            assert vid_id == "yt_vid_abc"

    def test_upload_video_quota_exceeded_error(self, tmp_path, monkeypatch):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"dummy video content")

        acct = yt.YouTubeAccount("cid", "sec", "ref")
        monkeypatch.setattr(yt, "get_access_token", lambda a: "mock_tok")

        init_resp = MagicMock()
        init_resp.status_code = 403
        init_resp.text = json.dumps({"error": {"errors": [{"reason": "quotaExceeded"}]}})

        with patch("httpx.Client.post", return_value=init_resp):
            with pytest.raises(yt.YouTubeError) as exc_info:
                yt.upload_video(
                    acct=acct,
                    video_path=video,
                    title="Amazing AI Sci-Fi",
                    description="Short story",
                    tags=["AI"],
                )
            assert "配额已耗尽" in str(exc_info.value)

    def test_publish_calls_upload_and_returns_url(self, tmp_path, monkeypatch):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"dummy")

        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "sec")
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "ref")

        monkeypatch.setattr(yt, "upload_video", lambda **kw: "test_123")
        url = yt.publish(
            video=video,
            title="My Title",
            description="My Desc",
            tags=["AI"],
        )
        assert url == "https://www.youtube.com/shorts/test_123"

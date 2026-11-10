"""测试抖音自研解析器 (douyin_api)。"""
from unittest.mock import MagicMock, patch
from pathlib import Path
from bot.source import douyin_api as da
from bot.source import downloader as dl


def test_is_douyin():
    assert da.is_douyin("https://www.douyin.com/video/7443315037764160825") is True
    assert da.is_douyin("https://v.douyin.com/oSMKKlfOY5A/") is True
    assert da.is_douyin("https://www.iesdouyin.com/share/video/123/") is True
    assert da.is_douyin("https://www.instagram.com/reel/123/") is False
    assert da.is_douyin("") is False


def test_extract_aweme_id():
    assert da.extract_aweme_id("https://www.douyin.com/video/7443315037764160825") == "7443315037764160825"
    assert da.extract_aweme_id("https://www.douyin.com/note/7443315037764160825") == "7443315037764160825"


@patch("bot.source.douyin_api._fetch_via_mobile_feed")
def test_extract_meta_via_douyin_api(mock_feed):
    mock_feed.return_value = {
        "aweme_id": "7443315037764160825",
        "desc": "老手艺人的坚守 #传统手艺 #解压",
        "author": {"nickname": "创手艺"},
        "video": {
            "duration": 249634,
            "play_addr": {
                "url_list": ["https://v3-dy-o.zjcdn.com/test_video.mp4"]
            },
            "cover": {
                "url_list": ["https://p3.douyinpic.com/cover.jpg"]
            }
        }
    }
    
    meta = da.extract("https://www.douyin.com/video/7443315037764160825")
    assert meta.video_id == "7443315037764160825"
    assert meta.username == "创手艺"
    assert meta.duration == 249.634
    assert meta.title.startswith("老手艺人的坚守")
    assert meta.platform == "douyin"


@patch("bot.source.douyin_api.extract")
def test_downloader_routes_to_douyin_api(mock_extract):
    from bot.source.downloader import VideoMeta
    mock_extract.return_value = VideoMeta(
        source_url="https://v.douyin.com/abc/",
        platform="douyin",
        video_id="123",
        shortcode="douyin_123",
        username="作者",
        title="测试标题",
        caption="描述",
        thumbnail_url="",
        duration=10.0,
        permalink="",
    )
    
    meta = dl.extract_meta("https://v.douyin.com/abc/")
    assert meta.video_id == "123"
    assert mock_extract.called

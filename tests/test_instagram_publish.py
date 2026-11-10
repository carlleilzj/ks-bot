"""IG 发布链路测试：Meta Graph API 三步流程 + 媒体公网托管 + caption 拼接。

重点覆盖「错了就会白发/发错」的约束：
1. caption 拼接顺序（第一行必须是钩子，hashtags 在末尾）；
2. 三步流程的顺序与参数（容器 → 轮询 → publish）；
3. 容器状态非 FINISHED 时绝不能进入发布；
4. 媒体 URL 签名校验（过期/篡改/路径穿越都必须拒绝）；
5. 两条 Meta 通道按 token 前缀自动切换 host（EAA→facebook / IGAA→instagram）。
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot.publish import instagram as ig
from bot.publish import media_upload as mu


# ---------- 通道识别 ----------

class TestGraphChannel:
    def test_igaa_token_uses_instagram_host(self):
        acct = ig.IgAccount(key="ig_pets", ig_user_id="1", access_token="IGAAxxxx")
        assert acct.graph_host == ig.GRAPH_HOST
        assert acct.channel == "instagram_login"

    def test_eaa_token_uses_facebook_host(self):
        """Facebook Login 的 token（EAA 开头）必须走 graph.facebook.com。

        混用 host 会得到「Cannot parse access token」这种误导性报错——
        2026-11-09 实测踩过：token 明明是有效的，只是域名不对。
        """
        acct = ig.IgAccount(key="ig_pets", ig_user_id="1", access_token="EAAP3Cxxxx")
        assert acct.graph_host == ig.GRAPH_HOST_FB
        assert acct.channel == "facebook_login"

    def test_masked_token_hides_secret(self):
        acct = ig.IgAccount(key="k", ig_user_id="1", access_token="EAAP3ClongtokenvalueZDZD")
        masked = acct.masked_token
        assert "longtokenvalue" not in masked
        assert masked.endswith("ZDZD")


# ---------- caption 拼接 ----------

class TestComposeCaption:
    def test_title_first_then_desc_then_tags(self):
        cap = ig.compose_caption("he had ONE job 😭", "the plot twist 💀",
                                 ["animation", "funnyvideos", "fyp"])
        lines = cap.split("\n")
        assert lines[0] == "he had ONE job 😭", "第一行必须是钩子"
        assert "the plot twist 💀" in cap
        assert cap.rstrip().endswith("#animation #funnyvideos #fyp")

    def test_tags_get_hash_prefix_and_dedupe(self):
        cap = ig.compose_caption("hook", "", ["#animation", "animation", "fyp"])
        assert cap.count("#animation") == 1
        assert "#fyp" in cap

    def test_empty_description_does_not_leave_blank_gap(self):
        cap = ig.compose_caption("hook only", "", ["fyp"])
        assert not cap.startswith("\n")
        assert "\n\n\n" not in cap

    def test_same_desc_as_title_is_not_duplicated(self):
        cap = ig.compose_caption("hook", "hook", [])
        assert cap.count("hook") == 1


# ---------- 三步发布流程 ----------

class TestPublishFlow:
    def _acct(self):
        return ig.IgAccount(key="ig_pets", ig_user_id="17841400000000000",
                            access_token="IGAAxxxx")

    def test_happy_path_order(self):
        """必须严格按 建容器 → 轮询 FINISHED → publish 的顺序执行。"""
        calls: list[str] = []
        with patch.object(ig, "load_account", return_value=self._acct()), \
             patch.object(ig, "check_publishing_quota", return_value=3), \
             patch.object(ig, "create_reel_container",
                          side_effect=lambda *a, **k: calls.append("create") or "cid_1"), \
             patch.object(ig, "wait_container_ready",
                          side_effect=lambda *a, **k: calls.append("wait") or "FINISHED"), \
             patch.object(ig, "publish_container",
                          side_effect=lambda *a, **k: calls.append("publish") or "mid_1"), \
             patch.object(ig, "fetch_permalink",
                          side_effect=lambda *a, **k: calls.append("permalink") or "https://insta/p/1"):
            url = ig.publish(Path("/tmp/x.mp4"), "hook", "desc", ["animation"],
                             account_key="ig_pets", media_url="https://cdn/x.mp4")

        assert calls == ["create", "wait", "publish", "permalink"]
        assert url == "https://insta/p/1"

    def test_container_not_finished_never_publishes(self):
        """容器处理失败时绝不能调 media_publish（否则会发出坏内容或报错打乱状态）。"""
        with patch.object(ig, "load_account", return_value=self._acct()), \
             patch.object(ig, "check_publishing_quota", return_value=0), \
             patch.object(ig, "create_reel_container", return_value="cid_1"), \
             patch.object(ig, "wait_container_ready",
                          side_effect=ig.InstagramError("容器处理失败 status=ERROR")), \
             patch.object(ig, "publish_container") as mock_publish:
            with pytest.raises(ig.InstagramError):
                ig.publish(Path("/tmp/x.mp4"), "hook", "", [],
                           account_key="ig_pets", media_url="https://cdn/x.mp4")
        mock_publish.assert_not_called()

    def test_missing_credentials_raises_login_expired(self):
        from bot.publish.base import LoginExpired
        with patch.object(ig, "load_account", return_value=None):
            with pytest.raises(LoginExpired) as e:
                ig.publish(Path("/tmp/x.mp4"), "hook", "", [], account_key="ig_pets")
        assert "IG_TOKEN_IG_PETS" in str(e.value)

    def test_quota_exhausted_blocks_publish(self):
        with patch.object(ig, "load_account", return_value=self._acct()), \
             patch.object(ig, "check_publishing_quota", return_value=100), \
             patch.object(ig, "create_reel_container") as mock_create:
            with pytest.raises(ig.InstagramError) as e:
                ig.publish(Path("/tmp/x.mp4"), "hook", "", [],
                           account_key="ig_pets", media_url="https://cdn/x.mp4")
        assert "上限" in str(e.value)
        mock_create.assert_not_called()

    def test_missing_account_key_raises(self):
        with pytest.raises(ig.InstagramError) as e:
            ig.publish(Path("/tmp/x.mp4"), "hook", "", [], account_key="")
        assert "account_key" in str(e.value)

    def test_wait_container_ready_returns_on_finished(self):
        resp = {"status_code": "FINISHED"}
        with patch.object(ig, "_request", return_value=resp), \
             patch.object(ig.time, "sleep"):
            status = ig.wait_container_ready(self._acct(), "cid", max_wait_sec=5)
        assert status == "FINISHED"

    def test_wait_container_ready_raises_on_error(self):
        resp = {"status_code": "ERROR", "status": "bad video"}
        with patch.object(ig, "_request", return_value=resp), \
             patch.object(ig.time, "sleep"):
            with pytest.raises(ig.InstagramError) as e:
                ig.wait_container_ready(self._acct(), "cid", max_wait_sec=5)
        assert "ERROR" in str(e.value)


# ---------- 媒体公网托管 ----------

class TestMediaToken:
    def test_roundtrip(self):
        token = mu.build_media_token("a/b.mp4", ttl=300)
        assert mu.verify_media_token("a/b.mp4", token)

    def test_rejects_tampered_path(self):
        token = mu.build_media_token("a/b.mp4", ttl=300)
        assert not mu.verify_media_token("a/OTHER.mp4", token)

    def test_rejects_expired(self):
        with patch.object(mu.time, "time", return_value=1000):
            token = mu.build_media_token("a/b.mp4", ttl=60)
        with patch.object(mu.time, "time", return_value=2000):
            assert not mu.verify_media_token("a/b.mp4", token)

    def test_rejects_garbage_token(self):
        assert not mu.verify_media_token("a/b.mp4", "not-a-token")
        assert not mu.verify_media_token("a/b.mp4", "")
        assert not mu.verify_media_token("a/b.mp4", "abc.def")


class TestBuildPublicUrl:
    def test_requires_base_url(self, tmp_path, monkeypatch):
        monkeypatch.delenv("IG_MEDIA_BASE_URL", raising=False)
        with pytest.raises(RuntimeError) as e:
            mu.build_public_url(tmp_path / "x.mp4", base_url="")
        assert "IG_MEDIA_BASE_URL" in str(e.value)

    def test_rejects_path_outside_final_dir(self, tmp_path, monkeypatch):
        """任意文件外泄防护：成品目录之外的文件必须拒绝。"""
        final_root = tmp_path / "final"
        final_root.mkdir()
        outside = tmp_path / "secret.mp4"
        outside.write_bytes(b"x")
        monkeypatch.setattr(mu, "FINAL_DIR", final_root)

        with pytest.raises(RuntimeError) as e:
            mu.build_public_url(outside, base_url="https://cdn.example.com")
        assert "成品目录" in str(e.value)

    def test_builds_signed_url_inside_final_dir(self, tmp_path, monkeypatch):
        final_root = tmp_path / "final"
        final_root.mkdir()
        video = final_root / "abc_final.mp4"
        video.write_bytes(b"video-bytes")
        monkeypatch.setattr(mu, "FINAL_DIR", final_root)

        url = mu.build_public_url(video, base_url="https://cdn.example.com")
        assert url.startswith("https://cdn.example.com/ig-media/")
        assert url.endswith("/abc_final.mp4")

        token = url.split("/ig-media/")[1].rsplit("/", 1)[0]
        assert mu.verify_media_token("abc_final.mp4", token)

    def test_media_handler_head_and_get(self, tmp_path, monkeypatch):
        final_root = tmp_path / "final"
        final_root.mkdir()
        video = final_root / "test_final.mp4"
        video.write_bytes(b"sample-video-content-here")
        monkeypatch.setattr(mu, "FINAL_DIR", final_root)

        token = mu.build_media_token("test_final.mp4")
        path = f"/ig-media/{token}/test_final.mp4"

        from unittest.mock import MagicMock
        handler = mu._MediaHandler.__new__(mu._MediaHandler)
        handler.path = path
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()

        handler.do_HEAD()
        handler.send_response.assert_called_with(200)
        assert handler.wfile.write.call_count == 0

        handler.do_GET()
        assert handler.wfile.write.call_count > 0


class TestPublishWorkerPlatformFiltering:
    def test_publish_worker_handles_ig_without_state_path(self, monkeypatch):
        from bot import publish_worker as pw
        from bot.config import Settings

        monkeypatch.setattr(pw, "maybe_process_login_requests", lambda base, s: None)
        monkeypatch.setattr(pw, "_pending_report_pairs", lambda: set())

        # 模拟 pending 任务返回一个 ig_pets job
        fake_task = {
            "task_id": 999,
            "shortcode": "test_ig",
            "platforms": [{"platform": "ig_pets"}],
        }
        monkeypatch.setattr(pw, "fetch_pending", lambda base, s: [fake_task])

        processed = []
        monkeypatch.setattr(pw, "process_platform",
                            lambda base, s, task, plat: processed.append(plat["platform"]))

        # 当 load_account 返回 None 时，不认领
        from bot.publish import instagram as ig
        monkeypatch.setattr(ig, "load_account", lambda key: None)

        s = Settings()
        n = pw.run_once("http://127.0.0.1:8765", s, only_platforms={"ig_pets"})
        assert n == 0
        assert processed == []

        # 当 load_account 返回有效账号时，认领并处理
        monkeypatch.setattr(ig, "load_account",
                            lambda key: ig.IgAccount(key, "123", "token"))
        n = pw.run_once("http://127.0.0.1:8765", s, only_platforms={"ig_pets"})
        assert n == 1
        assert processed == ["ig_pets"]

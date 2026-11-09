"""发布链路三项改进（借鉴 CreatorHub）的测试。

P0  作品链接从发布接口回包提取 photoId（免去爬管理页 30~90 秒）
P2  上传改用 file chooser（快手有多个 file input，直接取 first 会塞错）
P3  发布按钮过滤 disabled 态（转码未完成时点击无效但不报错）

这三项都是「错了会静默发出错误结果」的类型，必须锁死。
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from bot.publish import kuaishou as ks


# ---------- P0: 回包 photoId 提取 ----------

class TestPublishedUrlFromPayload:
    def test_photo_id_shortlink_key(self):
        """最常见形态：photoId 是 3x 开头的短链 key。"""
        payload = {"result": 1, "photoId": "3xabc123def"}
        assert ks._published_url_from_payload(payload) == \
            "https://www.kuaishou.com/short-video/3xabc123def"

    @pytest.mark.parametrize("key", ["photoId", "photo_id", "photoIdStr",
                                     "photoID", "workId", "work_id"])
    def test_all_known_field_names(self, key):
        payload = {"result": 1, key: "3xzz758qcvdybtg"}
        assert "3xzz758qcvdybtg" in ks._published_url_from_payload(payload)

    def test_nested_payload(self):
        """回包常是嵌套结构（data.result.photoId）。"""
        payload = {"result": 1, "data": {"result": {"photoId": "3xnested999"}}}
        assert "3xnested999" in ks._published_url_from_payload(payload)

    def test_list_payload(self):
        payload = [{"foo": "bar"}, {"photoId": "3xlist888"}]
        assert "3xlist888" in ks._published_url_from_payload(payload)

    def test_rejects_too_short_id(self):
        """短于 8 位的值不可信（可能是错误码/计数）。"""
        assert ks._published_url_from_payload({"photoId": "123"}) == ""

    def test_returns_empty_when_absent(self):
        assert ks._published_url_from_payload({"result": 1}) == ""
        assert ks._published_url_from_payload({}) == ""
        assert ks._published_url_from_payload(None) == ""
        assert ks._published_url_from_payload("string") == ""

    def test_payload_not_dict_inside(self):
        assert ks._published_url_from_payload({"data": "unexpected"}) == ""


class TestPublishResponseProbe:
    def test_captures_success_with_url(self):
        page = MagicMock()
        handlers = {}

        def on(event, cb):
            handlers[event] = cb

        page.on.side_effect = on
        state = ks._attach_publish_response_probe(page)
        assert "response" in handlers

        resp = MagicMock()
        resp.request.method = "POST"
        resp.url = "https://cp.kuaishou.com/rest/cp/works/photo/publish"
        resp.json.return_value = {"result": 1, "photoId": "3xprobe123"}

        handlers["response"](resp)

        assert state["seen"] is True
        assert state["ok"] is True
        assert state["url"] == "https://www.kuaishou.com/short-video/3xprobe123"

    def test_ignores_non_publish_requests(self):
        page = MagicMock()
        handlers = {}
        page.on.side_effect = lambda e, cb: handlers.update({e: cb})
        state = ks._attach_publish_response_probe(page)

        # GET 请求
        r1 = MagicMock()
        r1.request.method = "GET"
        r1.url = "https://cp.kuaishou.com/rest/cp/works/photo/publish"
        handlers["response"](r1)
        assert state["seen"] is False

        # 非 publish 路径
        r2 = MagicMock()
        r2.request.method = "POST"
        r2.url = "https://cp.kuaishou.com/rest/cp/works/photo/list"
        handlers["response"](r2)
        assert state["seen"] is False

    def test_ignores_config_endpoints(self):
        """config/snapshot/tips 也含 /publish 子串，必须排除。"""
        page = MagicMock()
        handlers = {}
        page.on.side_effect = lambda e, cb: handlers.update({e: cb})
        state = ks._attach_publish_response_probe(page)

        r = MagicMock()
        r.request.method = "POST"
        r.url = "https://cp.kuaishou.com/rest/cp/works/publish/config"
        handlers["response"](r)
        assert state["seen"] is False

    def test_captures_failure_without_url(self):
        page = MagicMock()
        handlers = {}
        page.on.side_effect = lambda e, cb: handlers.update({e: cb})
        state = ks._attach_publish_response_probe(page)

        r = MagicMock()
        r.request.method = "POST"
        r.url = "https://cp.kuaishou.com/rest/cp/works/photo/publish"
        r.json.return_value = {"result": 0, "message": "内容违规"}
        handlers["response"](r)

        assert state["seen"] is True
        assert state["ok"] is False
        assert state["url"] == ""
        assert state["message"] == "内容违规"

    def test_json_parse_error_does_not_crash(self):
        """回包不是 JSON（比如 502 HTML）时不能把监听器炸掉。"""
        page = MagicMock()
        handlers = {}
        page.on.side_effect = lambda e, cb: handlers.update({e: cb})
        state = ks._attach_publish_response_probe(page)

        r = MagicMock()
        r.request.method = "POST"
        r.url = "https://cp.kuaishou.com/rest/cp/works/photo/publish"
        r.json.side_effect = ValueError("not json")
        handlers["response"](r)   # 不应抛异常

        assert state["seen"] is False

    def test_only_first_response_counted(self):
        """并发/重试时可能有多个回包，只认第一个（避免被后续噪音覆盖）。"""
        page = MagicMock()
        handlers = {}
        page.on.side_effect = lambda e, cb: handlers.update({e: cb})
        state = ks._attach_publish_response_probe(page)

        r1 = MagicMock()
        r1.request.method = "POST"
        r1.url = "https://cp.kuaishou.com/rest/cp/works/photo/publish"
        r1.json.return_value = {"result": 1, "photoId": "3xfirst111"}
        handlers["response"](r1)

        r2 = MagicMock()
        r2.request.method = "POST"
        r2.url = "https://cp.kuaishou.com/rest/cp/works/photo/publish"
        r2.json.return_value = {"result": 0, "message": "second"}
        handlers["response"](r2)

        assert state["ok"] is True
        assert "3xfirst111" in state["url"]


# ---------- P2: file chooser 上传 ----------

class TestUploadViaChooser:
    def test_uses_file_chooser_when_button_exists(self):
        page = MagicMock()
        chooser = MagicMock()

        btn = MagicMock()
        btn.count.return_value = 1
        btn.nth.return_value.is_visible.return_value = True
        page.get_by_role.return_value = btn

        # expect_file_chooser 是上下文管理器
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.value = chooser
        page.expect_file_chooser.return_value = ctx

        ok = ks._upload_via_chooser(page, MagicMock(name="v.mp4"))
        assert ok is True
        chooser.set_files.assert_called_once()

    def test_returns_false_when_button_missing(self):
        page = MagicMock()
        btn = MagicMock()
        btn.count.return_value = 0
        page.get_by_role.return_value = btn

        video = MagicMock()
        assert ks._upload_via_chooser(page, video) is False


# ---------- P3: 发布按钮 disabled 过滤 ----------

class TestPublishButtonDisabled:
    def test_do_click_skips_disabled_and_retries(self):
        """按钮禁用时 _click_publish 不能立刻抛错，要重试等待。"""
        page = MagicMock()
        page.url = "https://cp.kuaishou.com/article/publish/video"

        # 前两次返回 disabled，第三次成功，然后模拟成功跳转
        calls = {"n": 0}

        def evaluate(js, *a):
            if "__ks_joyride_killer" in js and "isDisabled" in js:
                calls["n"] += 1
                return "disabled" if calls["n"] <= 2 else "ok"
            return ""

        page.evaluate.side_effect = evaluate
        page.get_by_text.return_value.count.return_value = 0
        page.locator.return_value.count.return_value = 0

        with patch.object(ks, "dismiss_dialogs"), \
             patch.object(ks, "_remove_joyride"), \
             patch.object(ks, "_visible_toast_text", return_value="发布成功"), \
             patch.object(ks, "_title_in_manage_list", return_value=True), \
             patch.object(ks, "shot"):
            # 模拟点击后离开发布页
            def wait(ms):
                page.url = "https://cp.kuaishou.com/article/manage"
            page.wait_for_timeout.side_effect = wait

            result = ks._click_publish(page, title="测试标题")

        # 至少尝试了 3 次才成功（前两次 disabled）
        assert calls["n"] >= 3
        assert isinstance(result, str)

    def test_raises_after_persistent_disabled(self):
        """持续 60 秒不可点 → 明确报错，不能谎报成功。"""
        page = MagicMock()
        page.url = "https://cp.kuaishou.com/article/publish/video"
        page.evaluate.return_value = "disabled"
        page.locator.return_value.count.return_value = 0
        page.get_by_text.return_value.count.return_value = 0

        with patch.object(ks, "dismiss_dialogs"), \
             patch.object(ks, "_remove_joyride"), \
             patch.object(ks, "shot"):
            with pytest.raises(ks.KuaishouError) as e:
                ks._click_publish(page, title="t")

        assert "不可点击" in str(e.value)
        assert "disabled" in str(e.value)

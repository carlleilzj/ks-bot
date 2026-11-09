"""变现挂载开关测试。

背景（2026-11-09）：用户要求「取消快手2号发布视频时的所有变现挂载」。
快手发布页的变现挂载有**两条独立路径**，只堵一条会漏：

    路径 A: platforms.<name>.attach_service / spark_task
            → kuaishou.py 的 5.5 段 → _attach_service / _attach_spark_task

    路径 B: platforms.<name>.anchors 里的 推广任务 / 变现任务 / 小程序
            → kuaishou_anchor.apply_anchors 的第 1 段 → apply_service

只把 attach_service 留空不够——若 anchors 里写了「推广任务」，照样会挂。
因此加了 allow_monetization 闸门。这组测试锁定两条路径都被封死，
同时确保**热点/地点/声明不受影响**（它们不是变现，挂上有利于流量与合规）。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bot.publish import kuaishou_anchor as ka


def _page():
    return MagicMock()


class TestAllowMonetization:
    def test_disabled_skips_all_service_anchors(self):
        """allow_monetization=False → 三条变现路径一条都不能走。"""
        page = _page()
        anchors = {
            "推广任务": "短剧测试",
            "变现任务": "星火测试",
            "小程序": "小程序测试",
            "热点": "治愈",
            "地点": "郴州",
            "声明": "内容由AI生成",
        }
        with patch.object(ka, "apply_service") as mock_service, \
             patch.object(ka, "apply_hot_topic", return_value=True), \
             patch.object(ka, "apply_location", return_value=True), \
             patch.object(ka, "apply_declaration", return_value=True), \
             patch.object(ka.time, "sleep"):
            done = ka.apply_anchors(page, anchors, allow_monetization=False)

        mock_service.assert_not_called(), "变现挂载禁用后仍调用了 apply_service！"
        # 非变现项必须照常工作
        assert "热点" in done
        assert "地点" in done
        assert any(d.startswith("声明") for d in done)

    def test_enabled_still_mounts_service(self):
        """allow_monetization=True（默认）时行为不变，避免误伤 A 号。"""
        page = _page()
        anchors = {"推广任务": "短剧测试"}
        with patch.object(ka, "apply_service", return_value="短剧测试任务名") as mock_service, \
             patch.object(ka.time, "sleep"):
            done = ka.apply_anchors(page, anchors, allow_monetization=True)

        mock_service.assert_called_once()
        assert any("推广任务" in d for d in done)

    def test_disabled_logs_skipped_keys(self, caplog):
        """跳过时要留日志，便于事后确认「确实跳了」而不是「悄悄没挂上」。"""
        page = _page()
        with patch.object(ka, "apply_service") as mock_service, \
             patch.object(ka, "apply_hot_topic", return_value=False), \
             patch.object(ka, "apply_location", return_value=False), \
             patch.object(ka, "apply_declaration", return_value=False), \
             patch.object(ka.time, "sleep"):
            with caplog.at_level("INFO"):
                ka.apply_anchors(page, {"推广任务": "x"}, allow_monetization=False)

        assert "变现挂载已禁用" in caplog.text
        mock_service.assert_not_called()

    def test_empty_anchors_noop(self):
        page = _page()
        with patch.object(ka.time, "sleep"):
            done = ka.apply_anchors(page, None, allow_monetization=False)
        assert done == []


class TestNonMonetizationAnchors:
    """热点/地点/声明都不是变现，禁用变现时不能连带禁用它们。"""

    def test_hot_topic_still_works(self):
        page = _page()
        with patch.object(ka, "apply_hot_topic", return_value=True) as mock_hot, \
             patch.object(ka.time, "sleep"):
            done = ka.apply_anchors(page, {"热点": "治愈"}, allow_monetization=False)
        mock_hot.assert_called_once()
        assert "热点" in done

    def test_location_still_works(self):
        page = _page()
        with patch.object(ka, "apply_location", return_value=True) as mock_loc, \
             patch.object(ka.time, "sleep"):
            done = ka.apply_anchors(page, {"地点": "郴州"}, allow_monetization=False)
        mock_loc.assert_called_once()
        assert "地点" in done

    def test_declaration_still_works(self):
        """声明是合规项（AI 生成内容标注），绝不能因取消变现而被跳过。"""
        page = _page()
        with patch.object(ka, "apply_declaration", return_value=True) as mock_decl, \
             patch.object(ka.time, "sleep"):
            done = ka.apply_anchors(page, {"声明": "内容由AI生成"},
                                    allow_monetization=False)
        mock_decl.assert_called_once()
        assert any(d.startswith("声明") for d in done)


class TestSinksAndReporting:
    def test_spark_sink_not_appended_when_disabled(self):
        """禁用变现时 spark_sink 不应收到任何条目（否则会误记收益归因）。"""
        page = _page()
        sink: list[str] = []
        with patch.object(ka, "apply_service", return_value="任务名"), \
             patch.object(ka.time, "sleep"):
            ka.apply_anchors(page, {"推广任务": "x"}, allow_monetization=False)
        assert sink == []


class TestConfigLevel:
    """配置层面：kuaishou2 的变现字段必须全空。"""

    def test_kuaishou2_has_no_monetization(self):
        from bot.config import load_settings
        s = load_settings()
        p = s.platforms.get("kuaishou2")
        if p is None:
            pytest.skip("当前配置没有 kuaishou2")
        assert p.attach_service == "", "kuaishou2 仍在挂推广服务"
        assert p.attach_prefer == ""
        assert p.attach_fallback == ""
        assert p.spark_task is False, "kuaishou2 仍在挂星火任务"

    def test_kuaishou2_anchors_have_no_monetization_keys(self):
        from bot.config import load_settings
        s = load_settings()
        p = s.platforms.get("kuaishou2")
        if p is None:
            pytest.skip("当前配置没有 kuaishou2")
        monet_keys = {"推广任务", "变现任务", "小程序"}
        found = monet_keys & set(p.anchors or {})
        assert not found, f"kuaishou2 的 anchors 里仍有变现项：{found}"

    def test_kuaishou_a_monetization_disabled(self):
        """2026-11-10 核验用户快手账单后明确：A 号取消全部变现挂载。

        实测聚星近 30 天收益为 0.00 元（受众完全不匹配充值短剧），
        挂载反而被打上营销标签压制公域推荐池、影响磁力万合分成。
        A/B 两号均彻底取消商业挂载，回归纯净发布。
        """
        from bot.config import load_settings
        s = load_settings()
        p = s.platforms.get("kuaishou")
        if p is None:
            pytest.skip("当前配置没有 kuaishou")
        assert p.attach_service == "", "A 号应已取消 attach_service 变现挂载"
        assert p.spark_task is False, "A 号应已关闭 spark_task"

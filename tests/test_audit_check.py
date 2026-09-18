"""抖音审核巡检回归测试。

背景（2026-09-19 实测）：
抖音创作者中心改版，内容管理页的审核状态筛选
从 `role=tab` 一排 tab 改成了**「审核状态」下拉筛选器**。
role=tab 数量变成 0，旧代码 get_by_role("tab") 命中 0 → 打一条
「未找到 tab」警告 → 返回空串 → 上层把空当"无违规" → 打印
「未发现违规作品/通知」。

**静默漏检**：不报错、不告警，还伪造"一切正常"。
实测页面上明明挂着两条「流量减少」的违规作品，
巡检连续 24 小时报告 0 违规。

修复要点（两条硬约束，务必保持）：
  1. 触发器**不能靠文字定位**。占位文本「审核状态」在选中一次后就被
     替换成当前值（如「未通过」），第二次调用找不到它 →
     必须用结构定位（工具栏里第 1 个 select-selection）。
  2. 选项要点 `.dy-creator-content-select-option`（semi-design 系），
     并派发 mousedown/mouseup/click 三连，裸 click() 不生效。
"""
from __future__ import annotations

import pytest


# ---------- 筛选器定位契约 ----------

class FakePage:
    """记录 evaluate 调用与回传值的假页面。"""

    def __init__(self, results=None, body="x" * 500):
        self._results = list(results or [])
        self._body = body
        self.evals: list[str] = []
        self.body_reads = 0

    def evaluate(self, script, arg=None):
        self.evals.append(script)
        return self._results.pop(0) if self._results else None

    def wait_for_timeout(self, ms):
        pass

    def keyboard(self):
        class K:
            def press(self, k): pass
        return K()

    def locator(self, sel):
        body = self._body

        class L:
            def inner_text(self, timeout=None): return body
            def count(self): return 0
        return L()

    def get_by_role(self, role, name=None):
        class L:
            def count(self): return 0
        return L()

    def get_by_text(self, t, exact=False):
        class L:
            def count(self): return 0
        return L()


class TestSwitchTabUsesStructureNotText:
    """触发器必须按结构定位，不能按文字。"""

    def test_first_call_uses_structure(self):
        """第 1 次调用（占位还是「审核状态」）必须走结构定位。"""
        from bot import audit_check as ac
        pg = FakePage(results=[True, True])   # 触发成功、点选成功
        body = ac._switch_tab(pg, "未通过")
        assert body, "应返回正文"
        # 第 1 个 evaluate 应是找 select-selection 的脚本
        assert "select-selection" in pg.evals[0], \
            "触发器必须用结构定位（select-selection），不能按文字找"
        assert "审核状态" not in pg.evals[0], \
            "不得按占位文字定位：选中一次后占位会被替换成当前值"

    def test_second_call_still_works_after_placeholder_replaced(self):
        """核心回归：连续筛两次，第二次占位已变成上一个选项值。

        旧实现用 get_by_text("审核状态") 定位，第二次必然失败 →
        「全部」永远筛不到 → 流量减少的违规作品永远漏检。
        """
        from bot import audit_check as ac
        pg = FakePage(results=[True, True])
        assert ac._switch_tab(pg, "未通过")
        pg2 = FakePage(results=[True, True])
        assert ac._switch_tab(pg2, "全部"), \
            "第二次调用（占位已被替换）也必须能筛"

    def test_option_click_dispatches_mouse_triple(self):
        """点选选项必须派发 mousedown/mouseup/click 三连。"""
        from bot import audit_check as ac
        pg = FakePage(results=[True, True])
        ac._switch_tab(pg, "未通过")
        opt_script = pg.evals[1]
        assert "mousedown" in opt_script and "mouseup" in opt_script, \
            "semi-design 系组件需要 mousedown 才能选中"
        assert "select-option" in opt_script, \
            "必须点 .dy-creator-content-select-option"

    def test_returns_empty_when_no_path_matches(self):
        """两条路径都没命中 → 返回空串（上层会告警，不静默）"""
        from bot import audit_check as ac
        pg = FakePage(results=[False, False])
        assert ac._switch_tab(pg, "全部") == ""


class TestFlaggedStatus:
    """流量减少等状态必须被识别为异常。"""

    def test_flagged_status_contains_traffic_reduced(self):
        from bot import audit_check as ac
        assert "流量减少" in ac.FLAGGED_STATUS
        assert "未通过" in ac.FLAGGED_STATUS
        assert "仅自己可见" in ac.FLAGGED_STATUS

    def test_parse_works_extracts_status(self):
        """从正文里解析出作品及其状态。"""
        from bot import audit_check as ac
        seg = (
            "00:44\n测试标题一\n编辑作品\n设置权限\n作品置顶\n删除作品\n"
            "2026年09月18日 12:08\n流量减少\n播放\n67\n"
            "00:30\n测试标题二\n编辑作品\n设置权限\n作品置顶\n删除作品\n"
            "2026年09月18日 10:03\n已发布\n播放\n476\n"
        )
        works = ac._parse_works(seg)
        assert len(works) == 2
        assert works[0]["status"] == "流量减少"
        assert works[0]["published_at"] == "2026年09月18日 12:08"
        assert works[1]["status"] == "已发布"


class TestReportCompatibility:
    """回报环节必须兼容作品类与通知类两种结果。"""

    def test_result_without_notice_key_does_not_crash(self):
        """回归：run_audit 的作品类结果没有 'notice' 键。

        旧代码硬取 r['notice'] → KeyError（实测在删除成功后崩在回报步骤）。
        """
        # 作品类结果的结构
        r = {"matched": {"title": "t", "published_at": "p", "task_id": 1},
             "cand": {"reason": "状态：流量减少", "title": "t"},
             "deleted": True}
        notice = r.get("notice") or {}
        cand = r.get("cand") or {}
        detail = (notice.get("text") or cand.get("reason")
                  or cand.get("title") or "")
        assert detail == "状态：流量减少"

    def test_notice_result_still_supported(self):
        r = {"notice": {"text": "违规通知正文"}, "matched": None}
        notice = r.get("notice") or {}
        cand = r.get("cand") or {}
        detail = (notice.get("text") or cand.get("reason")
                  or cand.get("title") or "")
        assert detail == "违规通知正文"

    def test_empty_result_yields_empty_detail(self):
        r = {}
        notice = r.get("notice") or {}
        cand = r.get("cand") or {}
        detail = (notice.get("text") or cand.get("reason")
                  or cand.get("title") or "")
        assert detail == ""

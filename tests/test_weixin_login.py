"""视频号登录检测回归测试。

背景（2026-09-17 实测发现）：登录成功后 _is_logged_in 仍返回 False。
两个叠加的误判：
  1. `[class*='qrcode']` 在**已登录**的后台页面上也命中 3 个框架残留节点
     → 恒判未登录
  2. URL 判据 `"/platform/" in url` 漏掉不带尾斜杠的 `/platform`
     （登录后重定向到的正是这个地址）
后果：发布 worker 会把有效登录态当成过期，任务被 SKIPPED 成「登录态失效」。
"""
from __future__ import annotations

import pytest

from bot.publish import weixin as wx


class FakeLocator:
    def __init__(self, count=0):
        self._count = count

    def count(self):
        return self._count


class FakePage:
    """可配置各选择器命中数的假页面。"""

    def __init__(self, url="https://channels.weixin.qq.com/platform",
                 text_hits=0, qr_visible=0, file_input=0):
        self.url = url
        self._text_hits = text_hits
        self._qr_visible = qr_visible
        self._file_input = file_input
        self.queried: list[str] = []

    def get_by_text(self, t, exact=False):
        return FakeLocator(self._text_hits)

    def locator(self, sel):
        self.queried.append(sel)
        if "qrcode" in sel:
            return FakeLocator(self._qr_visible)
        if "file" in sel:
            return FakeLocator(self._file_input)
        return FakeLocator(0)


# ---------- 已登录的判定 ----------

def test_logged_in_on_platform_url_without_trailing_slash():
    """回归：/platform（无尾斜杠）必须判为已登录。

    登录后就是重定向到这个地址；旧代码写的是 "/platform/" in url，漏判。
    """
    page = FakePage(url="https://channels.weixin.qq.com/platform")
    assert wx._is_logged_in(page) is True


def test_logged_in_on_platform_subpage():
    page = FakePage(url="https://channels.weixin.qq.com/platform/post/list")
    assert wx._is_logged_in(page) is True


def test_logged_in_via_file_input_on_other_url():
    """非 platform 路径但有上传输入框 → 也算已登录。"""
    page = FakePage(url="https://channels.weixin.qq.com/some/other", file_input=1)
    assert wx._is_logged_in(page) is True


# ---------- 未登录的判定 ----------

def test_not_logged_in_when_url_has_login():
    page = FakePage(url="https://channels.weixin.qq.com/login.html")
    assert wx._is_logged_in(page) is False


def test_not_logged_in_when_url_has_passport():
    page = FakePage(url="https://passport.weixin.qq.com/xyz")
    assert wx._is_logged_in(page) is False


def test_not_logged_in_when_login_text_present():
    page = FakePage(text_hits=1)
    assert wx._is_logged_in(page) is False


def test_not_logged_in_when_visible_qr_present():
    page = FakePage(url="https://channels.weixin.qq.com/", qr_visible=1)
    assert wx._is_logged_in(page) is False


# ---------- 关键：二维码选择器必须要求 visible ----------

def test_qr_selector_requires_visible():
    """回归：查询二维码的选择器必须带 :visible。

    旧代码用 [class*='qrcode']，在已登录后台也能命中残留节点，导致恒判未登录。
    """
    # 用非 platform URL，确保走到二维码分支（platform URL 会提前返回 True）
    page = FakePage(url="https://channels.weixin.qq.com/some/other",
                    qr_visible=0, file_input=1)
    assert wx._is_logged_in(page) is True
    qr_queries = [s for s in page.queried if "qrcode" in s]
    assert qr_queries, "应当查询过二维码选择器"
    for sel in qr_queries:
        assert ":visible" in sel, f"二维码选择器必须要求可见：{sel!r}"
        assert "[class*='qrcode']" not in sel, "不得使用过宽的 class 通配"


def test_logged_in_page_with_stale_qr_nodes_not_misjudged():
    """已登录后台上残留的二维码节点（不可见）不得导致误判未登录。"""
    page = FakePage(url="https://channels.weixin.qq.com/platform", qr_visible=0)
    assert wx._is_logged_in(page) is True


def test_exception_returns_false():
    class Boom(FakePage):
        def locator(self, sel):
            raise RuntimeError("page detached")

    assert wx._is_logged_in(Boom()) is False

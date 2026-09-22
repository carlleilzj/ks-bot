"""抖音挂载（douyin_anchor）回归测试。

背景：2026-09-17 修复两个叠加的 bug：
  1. semi-select 靠 mousedown 展开；Playwright 的 click() 会等到 30s 超时
     → 每次发布白等 30 秒（日志 `[douyin_anchor] 打开挂载下拉失败`）
  2. 选项选择器匹配到了 `div.select-dropdown-option-video`（那是挂载类型
     下拉的**触发器预留节点**，不是选项），导致「点了但没选中」的假成功
     真实选项在 `div.semi-select-option` 里，文案在 `.semi-select-option-text`
"""
from __future__ import annotations

import pytest

from bot.publish import douyin_anchor as da


class FakePage:
    """记录 evaluate 调用的假页面。"""

    def __init__(self, eval_results=None, sel_texts=None, options=None,
                 click_ok=True, click_result=None):
        self.eval_results = list(eval_results or [])
        self.calls: list[tuple[str, object]] = []
        self.sel_texts = sel_texts or []
        self.options = options           # 选项查询固定返回这个
        self.click_ok = click_ok         # 点击（命中）固定返回这个
        self.click_result = click_result  # 非 None 时覆盖点击返回值（模拟未命中）
        self.waits = 0
        self.keys: list[str] = []

    def evaluate(self, script, arg=None):
        self.calls.append((script, arg))
        # 按脚本内容分派：选项查询 / 点击选项 / 其他
        if "_visible_options" in repr(type(self)) or True:
            if ".semi-select-option" in script and "return out" in script:
                if self.options is not None:
                    return list(self.options)
            if "dispatchEvent" in script and "semi-select-option" in script:
                if self.click_result is not None:
                    return self.click_result
                return self.click_ok
        if self.eval_results:
            return self.eval_results.pop(0)
        return None

    def wait_for_timeout(self, ms):
        self.waits += 1

    def keyboard(self):
        page = self

        class K:
            def type(self, s, delay=0):
                page.keys.append(s)

            def press(self, k):
                pass

        return K()

    def locator(self, sel):
        texts = self.sel_texts

        class L:
            def count(self):
                return len(texts)

            def nth(self, i):
                outer = self

                class N:
                    def inner_text(self):
                        return texts[i]

                    def evaluate(self, *a, **k):
                        return None

                return N()

        return L()


# ---------- 选项选择器必须排除触发器预留节点 ----------

def test_visible_options_query_targets_real_options():
    """_visible_options 的 DOM 查询必须用 .semi-select-option，
    不能再用 [class*='select-dropdown-option']（那是触发器节点）。"""
    page = FakePage(options=["位置", "带货模式"])
    out = da._visible_options(page)
    assert out == ["位置", "带货模式"]
    script = page.calls[0][0]
    assert ".semi-select-option" in script
    assert "select-dropdown-option" not in script, \
        "不得再匹配 select-dropdown-option-video（触发器预留节点）"
    assert "semi-select-option-text" in script, "文案应从 .semi-select-option-text 取"


def test_visible_options_filters_invisible():
    """查询脚本必须过滤不可见节点（offsetParent/clientRects）。"""
    page = FakePage(options=[])
    da._visible_options(page)
    script = page.calls[0][0]
    assert "offsetParent" in script and "getClientRects" in script


def test_visible_options_returns_empty_on_error():
    """evaluate 抛异常时安全返回空列表。"""
    class Boom(FakePage):
        def evaluate(self, *a, **k):
            raise RuntimeError("page detached")

    assert da._visible_options(Boom()) == []


# ---------- 点击选项 ----------

def test_click_option_dispatches_mouse_triple():
    """_click_option 必须在真实 option 节点上派发 mousedown/mouseup/click。"""
    page = FakePage(eval_results=[True])
    assert da._click_option(page, "带货模式") is True
    script = page.calls[0][0]
    for ev in ("mousedown", "mouseup", "click"):
        assert ev in script
    assert ".semi-select-option" in script
    assert page.calls[0][1] == "带货模式"


def test_click_option_returns_false_when_no_match():
    """选项列表里没有目标 → 返回 False。"""
    page = FakePage(click_result=False)
    assert da._click_option(page, "不存在的选项") is False


def test_click_option_handles_exception():
    class Boom(FakePage):
        def evaluate(self, *a, **k):
            raise RuntimeError("boom")

    assert da._click_option(Boom(), "x") is False


# ---------- open_semi_select ----------

def test_open_semi_select_dispatches_to_container():
    """展开必须在 .semi-select 容器上派发 mousedown（不是 click()）。"""
    captured = {}

    class L:
        def evaluate(self, script):
            captured["script"] = script

    page = FakePage()
    assert da.open_semi_select(L(), page, settle_ms=10) is True
    assert "mousedown" in captured["script"]
    assert ".semi-select" in captured["script"]


def test_open_semi_select_handles_failure():
    class L:
        def evaluate(self, script):
            raise RuntimeError("nope")

    assert da.open_semi_select(L(), FakePage()) is False


# ---------- 回读校验 ----------

def test_verified_true_when_type_and_value_present():
    """类型栏=位置、值栏=具体地点 → 校验通过。"""
    page = FakePage(sel_texts=["合集", "请选择合集", "位置", "带货模式",
                               "郴州友阿国际广场", "点击输入热点词"])
    assert da._verified(page, "位置", "郴州") is True


def test_verified_false_when_value_still_placeholder():
    """值栏仍是「输入地理位置」→ 没填上，校验必须失败（这是曾经的假成功）。"""
    page = FakePage(sel_texts=["合集", "请选择合集", "位置", "带货模式",
                               "输入地理位置", "点击输入热点词"])
    assert da._verified(page, "位置", "郴州") is False


def test_verified_false_when_type_not_selected():
    """类型栏还没变成「位置」→ 校验失败。"""
    page = FakePage(sel_texts=["合集", "请选择合集", "带货模式",
                               "输入地理位置", "点击输入热点词"])
    assert da._verified(page, "位置", "郴州") is False


def test_verified_false_when_value_mismatch():
    """值栏填的是别的地点 → 校验失败。"""
    page = FakePage(sel_texts=["合集", "请选择合集", "位置", "带货模式",
                               "北京天安门", "点击输入热点词"])
    assert da._verified(page, "位置", "郴州") is False


def test_verified_false_for_unknown_type():
    """未知挂载类型 → 校验失败（无 target 可比对）。"""
    page = FakePage(sel_texts=["位置"])
    assert da._verified(page, "不存在的类型", "x") is False


# ---------- _pick_dropdown_option 的 verify 契约 ----------

def test_pick_retries_when_verify_fails():
    """点了但回读未确认 → 必须重试，不能返回 True。

    这是本次修复的核心：点击动作的返回值不可信，只有回读算数。
    """
    # 点成功但回读恒 False → 必须重试到超时并返回 False
    page = FakePage(options=["带货模式"], click_ok=True)
    assert da._pick_dropdown_option(page, "带货模式", timeout_ms=0,
                                    verify=lambda: False) is False


def test_pick_returns_true_when_verify_passes():
    page = FakePage(options=["位置"], click_ok=True)
    assert da._pick_dropdown_option(page, "位置", timeout_ms=0,
                                    verify=lambda: True) is True


def test_pick_without_verify_trusts_click():
    """不传 verify 时保持旧语义（兼容其他调用点）。"""
    page = FakePage(options=["位置"], click_ok=True)
    assert da._pick_dropdown_option(page, "位置", timeout_ms=0) is True


def test_pick_goods_for_nationwide_default():
    assert da.pick_goods_for("搞笑动画无声视频") == "抽纸"
    assert da.pick_goods_for("") == "抽纸"


def test_pick_goods_for_pet_still_dogfood():
    assert da.pick_goods_for("小猫追毛线") == "狗粮"


def test_search_anchor_types_include_goods():
    assert "商品" in da.SEARCH_ANCHOR_TYPES
    assert "标记万物" in da.SEARCH_ANCHOR_TYPES
    assert da.DEFAULT_NATIONWIDE_GOODS == "抽纸"


def test_apply_anchors_auto_resolves_to_tissue(monkeypatch):
    """商品=auto → 抽纸，并走搜索框路径。"""
    called = {}

    def fake_search(page, keyword):
        called["kw"] = keyword
        return True

    monkeypatch.setattr(da, "apply_tag_search", fake_search)
    monkeypatch.setattr(da, "apply_anchor", lambda *a, **k: False)
    monkeypatch.setattr(da, "apply_hot_topic", lambda *a, **k: False)
    out = da.apply_anchors(FakePage(), {"商品": "auto"}, hot_topic="")
    assert out == ["商品"]
    assert called["kw"] == "抽纸"


def test_tag_search_verified_matches_keyword():
    page = FakePage(sel_texts=["抽纸 维达 120抽"])
    assert da._tag_search_verified(page, "抽纸", "维达抽纸家庭装") is True

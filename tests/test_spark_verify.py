"""星火挂载回读校验的回归测试。

背景：2026-09-13 起出现「交互全部成功、日志报『已挂星火变现任务』、
但平台端作品不带『流量助推』」的静默失效。根因是发布器只相信交互动作的
返回值，从不回读表单。本测试锁死新的回读校验行为：
挂载未生效时 `_attach_spark_task` 必须返回 None，调用方必须阻断发布。
"""
from __future__ import annotations

import pytest

import bot.publish.kuaishou as ks


class FakePage:
    """最小 Playwright Page 替身：只需要 evaluate / get_by_text / keyboard 等。"""

    def __init__(self, picks=None, dropdown=None, task_hint=""):
        self._picks = picks or []
        self._dropdown = dropdown or []
        self._task_hint = task_hint
        self.escaped = 0
        self._deadline = 0

    # ---- 供 _spark_form_state 调用 ----
    def evaluate(self, script, arg=None):
        if "ant-select-selection-item" in script:
            out = {"picks": list(self._picks), "service_type": "", "task": ""}
            for t in self._picks:
                if "关联变现任务" in t and not out["service_type"]:
                    out["service_type"] = t
                elif not out["task"]:
                    out["task"] = t
            if self._task_hint:
                out["task_hint"] = self._task_hint
            return out
        if "ant-select-item-option" in script and "includes" in script:
            # _select_spark_option 的下拉兜底点击
            return any((arg or "") in o for o in self._dropdown)
        if "ant-select-item-option" in script and "find" in script:
            return any((arg or "") == o for o in self._dropdown)
        return None

    def inner_text(self, sel):
        # _attach_spark_task 用它兜底确认下拉/页面里有「关联变现任务」文案
        return "关联变现任务" if self._picks or self._dropdown else ""

    def get_by_text(self, text, exact=False):
        return FakeLocator(0)

    def keyboard(self):
        return self

    def press(self, key):
        self.escaped += 1

    def wait_for_timeout(self, ms):
        return None


class FakeLocator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count

    def last(self):
        return self

    def click(self, **kw):
        return None


# ---------- _spark_attached：回读判定 ----------

def test_attached_true_when_pick_contains_title():
    """表单选中项里出现任务标题特征 → 判定已挂上。"""
    page = FakePage(picks=["关联变现任务", "狐缘山间任务时间：2026.05.06-2027.05.31"])
    assert ks._spark_attached(page, "狐缘山间") is True


def test_attached_true_with_platform_suffix():
    """平台给标题追加「任务时间：…」后缀时，前缀匹配仍能命中。"""
    page = FakePage(picks=["关联变现任务", "扫墓丫头，替祖宗横扫天下任务时间：2026.05.14-2027.05.31"])
    assert ks._spark_attached(page, "扫墓丫头，替祖宗横扫天下") is True


def test_attached_false_when_only_service_type_selected():
    """只选中了服务类型『关联变现任务』、任务栏为空 → 未生效（本次事故的形态）。"""
    page = FakePage(picks=["关联变现任务"])
    assert ks._spark_attached(page, "狐缘山间") is False


def test_attached_false_when_form_untouched():
    """表单没有任何选中项 → 未生效。"""
    page = FakePage(picks=[])
    assert ks._spark_attached(page, "狐缘山间") is False


@pytest.mark.parametrize("ph", ks.SPARK_TASK_PLACEHOLDERS)
def test_attached_false_when_placeholder_still_showing(ph):
    """任务栏仍是占位文案（新旧两版）→ 不算挂上。"""
    page = FakePage(picks=["关联变现任务", ph])
    assert ks._spark_attached(page, "狐缘山间") is False


def test_attached_false_when_wrong_task_selected():
    """挂上的是别的任务、不是我们想挂的那个 → 必须判为未生效。"""
    page = FakePage(picks=["关联变现任务", "完全无关的另一个任务"])
    assert ks._spark_attached(page, "狐缘山间") is False


def test_attached_false_when_pre_state_unchanged():
    """chosen 为空时，任务栏与挂载前一样 → 未变动，不算挂上。"""
    page = FakePage(picks=["关联变现任务", "某个任务"])
    pre = {"task": "某个任务"}
    assert ks._spark_attached(page, "", pre_state=pre) is False


def test_attached_true_when_pre_state_changed():
    """chosen 为空但任务栏确实变了 → 放行。"""
    page = FakePage(picks=["关联变现任务", "新挂上的任务"])
    pre = {"task": ""}
    assert ks._spark_attached(page, "", pre_state=pre) is True


def test_attached_tolerates_platform_suffix_stripping():
    """下拉项带『任务时间：…』后缀，选中后表单只显示任务名 —— 双向匹配都要命中。"""
    # 表单里显示纯任务名，chosen 是带后缀的下拉项
    page = FakePage(picks=["关联变现任务", "狐缘山间"])
    assert ks._spark_attached(page, "狐缘山间任务时间：2026.05.06-2027.05.31") is True


def test_spark_title_probe_strips_suffix():
    """_spark_title_probe 去掉『任务时间：…』后缀。"""
    assert ks._spark_title_probe(None, "狐缘山间任务时间：2026.05.06-2027.05.31") == "狐缘山间"
    assert ks._spark_title_probe(None, "狐缘山间") == "狐缘山间"


def test_attached_ignores_service_type_field():
    """picks 里只有服务类型那一栏（『关联变现任务』）不算挂上具体任务。"""
    page = FakePage(picks=["关联变现任务"])
    assert ks._spark_attached(page, "狐缘山间") is False


def test_form_state_survives_evaluate_exception():
    """回读本身抛异常时返回空 dict，不把异常往上抛。"""
    class Broken(FakePage):
        def evaluate(self, script, arg=None):
            raise RuntimeError("detached")

    st = ks._spark_form_state(Broken())
    assert st == {}
    assert ks._spark_attached(Broken(), "狐缘山间") is False


# ---------- 端到端：挂载失败必须阻断发布 ----------

def test_attach_returns_none_when_no_entry(monkeypatch):
    """服务类型下拉打不开（选项为空）→ 返回 None（调用方将阻断发布）。"""
    page = FakePage()
    monkeypatch.setattr(ks, "_open_select_dropdown", lambda p, ph="": [])
    monkeypatch.setattr(ks, "shot", lambda *a, **k: None)
    monkeypatch.setattr(ks, "dismiss_dialogs", lambda p: None)
    monkeypatch.setattr(ks, "_remove_joyride", lambda p: None)
    assert ks._attach_spark_task(page) is None


def test_attach_returns_none_when_type_missing(monkeypatch):
    """作者服务下拉里没有『关联变现任务』→ 返回 None。"""
    page = FakePage()
    monkeypatch.setattr(ks, "_open_select_dropdown",
                        lambda p, ph="": ["关联商品", "关联小程序"])
    monkeypatch.setattr(ks, "shot", lambda *a, **k: None)
    monkeypatch.setattr(ks, "dismiss_dialogs", lambda p: None)
    monkeypatch.setattr(ks, "_remove_joyride", lambda p: None)
    assert ks._attach_spark_task(page) is None


def test_attach_returns_none_when_verify_fails(monkeypatch):
    """核心回归：点选动作全部成功，但回读未确认 → 必须返回 None。"""
    page = FakePage(picks=["关联变现任务"])  # 任务栏空 → 回读失败
    monkeypatch.setattr(ks, "dismiss_dialogs", lambda p: None)
    monkeypatch.setattr(ks, "_remove_joyride", lambda p: None)
    def _open(p, ph=""):
        # 第二层（占位符含"收益"）→ 任务列表；第一层 → 全部服务类型
        if any(x in ph for x in ks.SPARK_TASK_PLACEHOLDERS):
            return ["狐缘山间"]
        return ["关联商品", ks.SPARK_TYPE_LABEL, "关联小程序"]
    monkeypatch.setattr(ks, "_open_select_dropdown", _open)
    monkeypatch.setattr(ks, "_click_visible_option", lambda p, t: True)
    monkeypatch.setattr(ks, "_select_spark_option", lambda p, t: True)
    monkeypatch.setattr(ks, "shot", lambda *a, **k: None)
    monkeypatch.setattr(ks, "_spark_rr_load", lambda: 0)
    saved = []
    monkeypatch.setattr(ks, "_spark_rr_save", lambda c: saved.append(c))

    assert ks._attach_spark_task(page, prefer="狐缘山间") is None
    assert saved == [], "回读失败时不应推进轮转游标"


def test_attach_returns_title_when_verify_passes(monkeypatch):
    """回读确认后返回标题，并推进轮转游标。"""
    page = FakePage(picks=["关联变现任务", "狐缘山间任务时间：2026.05.06-2027.05.31"])
    monkeypatch.setattr(ks, "dismiss_dialogs", lambda p: None)
    monkeypatch.setattr(ks, "_remove_joyride", lambda p: None)
    def _open(p, ph=""):
        # 第二层（占位符含"收益"）→ 任务列表；第一层 → 全部服务类型
        if any(x in ph for x in ks.SPARK_TASK_PLACEHOLDERS):
            return ["狐缘山间"]
        return ["关联商品", ks.SPARK_TYPE_LABEL, "关联小程序"]
    monkeypatch.setattr(ks, "_open_select_dropdown", _open)
    monkeypatch.setattr(ks, "_click_visible_option", lambda p, t: True)
    monkeypatch.setattr(ks, "_select_spark_option", lambda p, t: True)
    monkeypatch.setattr(ks, "shot", lambda *a, **k: None)
    monkeypatch.setattr(ks, "_spark_rr_load", lambda: 0)
    saved = []
    monkeypatch.setattr(ks, "_spark_rr_save", lambda c: saved.append(c))

    assert ks._attach_spark_task(page, prefer="狐缘山间") == "狐缘山间"
    assert len(saved) == 1, "回读成功才推进游标"


# ---------- 异常类型契约 ----------

def test_spark_attach_error_is_publish_error():
    """SparkAttachError 必须是 PublishError 子类，worker 才能按发布失败回报。"""
    from bot.publish.base import PublishError
    assert issubclass(ks.SparkAttachError, PublishError)


def test_spark_attach_error_carries_message():
    e = ks.SparkAttachError("星火未生效")
    assert "星火未生效" in str(e)

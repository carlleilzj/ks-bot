"""回归测试：发布成功判定与 anchors 挂载接通（2026-11-04 深度审计修复）。"""

from __future__ import annotations

from unittest.mock import MagicMock

from bot.publish import kuaishou as ks

# ---------- 成功判定不能假阳性 ----------

def test_success_texts_exclude_pending_review():
    """「审核中」不能算成功：发布页本身就可能出现该字样。"""
    assert "审核中" not in ks.SELECTORS["success_texts"]
    assert "发布成功" in ks.SELECTORS["success_texts"]


def test_visible_toast_text_only_reads_containers():
    """只读 toast/弹窗容器，不扫整页 —— 否则「请勿发布违规内容」这类常驻文案会误判。"""
    page = MagicMock()
    toast = MagicMock()
    toast.count.return_value = 1
    toast.nth.return_value = toast
    toast.is_visible.return_value = True
    toast.inner_text.return_value = "操作过于频繁，请稍后再试"

    def locator(sel):
        if "ant-message" in sel:
            return toast
        m = MagicMock()
        m.count.return_value = 0
        return m

    page.locator.side_effect = locator
    text = ks._visible_toast_text(page)
    assert "操作过于频繁" in text


def test_visible_toast_text_ignores_hidden():
    page = MagicMock()
    hidden = MagicMock()
    hidden.count.return_value = 1
    hidden.nth.return_value = hidden
    hidden.is_visible.return_value = False
    page.locator.return_value = hidden
    assert ks._visible_toast_text(page) == ""


# ---------- 标题回查不能拿整页 in ----------

def _manage_body(title: str, when: str = "2026-11-04 10:00:00") -> str:
    return f"共338个作品\n00:20\n{title}\n{when}\n 5\n 0\n 0\n"


def test_title_in_manage_list_matches_card():
    page = MagicMock()
    page.evaluate.return_value = _manage_body("唤出万年神龙动静这么大，闹半天就为了借火烤肠 #3D动画")
    assert ks._title_in_manage_list(page, "唤出万年神龙动静这么大，闹半天就为了借火烤肠") is True


def test_title_in_manage_list_rejects_short_title():
    """标题清洗后不足 6 字无法可靠区分，宁可判否（避免假阳性记成发布成功）。"""
    page = MagicMock()
    page.evaluate.return_value = _manage_body("短标题")
    assert ks._title_in_manage_list(page, "短标题") is False


def test_title_in_manage_list_no_false_positive_across_cards():
    page = MagicMock()
    page.evaluate.return_value = _manage_body("完全不相干的另一个作品标题在这里")
    assert ks._title_in_manage_list(page, "唤出万年神龙动静这么大") is False


def test_title_in_manage_list_tolerates_punctuation():
    page = MagicMock()
    page.evaluate.return_value = _manage_body("连鞋都顾不上穿，母鸡带全家在旁边当裁判 #3D动画")
    assert ks._title_in_manage_list(page, "连鞋都顾不上穿，母鸡带全家在旁边当裁判") is True


# ---------- 首评错位防护 ----------

def test_post_first_comment_bails_when_title_too_short():
    """标题清洗后过短必须直接放弃，绝不能因为「空串 in 任意串」而发到上一条作品。"""
    page = MagicMock()
    ok = ks.post_first_comment(page, "测试首评", title="短", pin=False)
    assert ok is False
    page.goto.assert_not_called()


def test_post_first_comment_bails_when_title_empty():
    page = MagicMock()
    assert ks.post_first_comment(page, "测试首评", title="", pin=False) is False
    page.goto.assert_not_called()


def test_post_first_comment_empty_comment_returns_false():
    page = MagicMock()
    assert ks.post_first_comment(page, "   ", title="一个足够长的标题用来核对") is False
    page.goto.assert_not_called()


# ---------- anchors 参数存在且软降级 ----------

def test_publish_accepts_anchors_kwarg():
    import inspect

    sig = inspect.signature(ks.publish)
    assert "anchors" in sig.parameters
    assert "first_comment" in sig.parameters
    assert "pin_comment" in sig.parameters

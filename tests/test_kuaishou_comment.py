"""测试快手作者置顶首评（神评互动引导）生成与发布。"""

from unittest.mock import MagicMock

from bot.ai.copywriter import PLATFORM_PROFILES, _system_prompt, _validate
from bot.publish.kuaishou import post_first_comment
from bot.publish_worker import _platform_copy


def test_first_comment_prompt_and_validate():
    prof_a = PLATFORM_PROFILES["kuaishou"]
    prompt_a = _system_prompt(prof_a)
    assert "作者置顶首评（first_comment）" in prompt_a
    assert "神龙：我几千年的修为" in prompt_a

    prof_b = PLATFORM_PROFILES["kuaishou2"]
    prompt_b = _system_prompt(prof_b)
    assert "作者置顶首评（first_comment）" in prompt_b
    assert "手残党亲测" in prompt_b

    # 测试 _validate 解析并清洗首评
    raw = {
        "title": "小恐龙烤香肠喷出大火球",
        "description": "小龙打了个喷嚏把香肠全烧成黑炭了。",
        "tags": ["3D动画", "小龙烤肠", "脑洞大开", "搞笑日常"],
        "category": "搞笑",
        "first_comment": "【神龙：我几千年的修为，你拿来烤淀粉肠？】",
    }
    out = _validate(raw, ["搞笑"], prof_a)
    assert out["first_comment"] == "神龙：我几千年的修为，你拿来烤淀粉肠？"
    assert "【" not in out["first_comment"]


def test_platform_copy_extracts_first_comment():
    task = {
        "title": "任务标题",
        "copy_json": '{"kuaishou": {"title": "A号标题", "description": "A号描述", "tags": ["t1"], "first_comment": "神评A"}, "kuaishou2": {"title": "B号标题", "first_comment": "神评B"}}',
    }
    copy_a = _platform_copy(task, "kuaishou")
    assert copy_a["title"] == "A号标题"
    assert copy_a["first_comment"] == "神评A"

    copy_b = _platform_copy(task, "kuaishou2")
    assert copy_b["title"] == "B号标题"
    assert copy_b["first_comment"] == "神评B"


def test_post_first_comment_mock():
    mock_page = MagicMock()
    mock_box = MagicMock()
    mock_box.count.return_value = 1
    mock_box.is_visible.return_value = True
    mock_box.inner_text.return_value = ""

    mock_btn = MagicMock()
    mock_btn.count.return_value = 1
    mock_btn.is_visible.return_value = True

    mock_pin_btn = MagicMock()
    mock_pin_btn.count.return_value = 1
    mock_pin_btn.is_visible.return_value = True

    mock_confirm_btn = MagicMock()
    mock_confirm_btn.count.return_value = 1
    mock_confirm_btn.is_visible.return_value = True

    mock_title = MagicMock()
    mock_title.count.return_value = 1
    mock_title.inner_text.return_value = "测试视频标题"
    mock_title.first = mock_title

    mock_item = MagicMock()
    mock_item.count.return_value = 1
    mock_item.locator.return_value.first = mock_pin_btn
    mock_item.first = mock_item

    mock_box.first = mock_box
    mock_btn.first = mock_btn
    mock_pin_btn.first = mock_pin_btn
    mock_confirm_btn.first = mock_confirm_btn

    def locator_side_effect(selector):
        if "title" in selector:
            return mock_title
        if "author-comment-input__row__input" in selector:
            return mock_box
        if "author-comment-input__row__btn" in selector:
            return mock_btn
        if "comment-item" in selector:
            mock_ret = MagicMock()
            mock_ret.filter.return_value = mock_item
            mock_ret.count.return_value = 1
            return mock_ret
        if "置顶" in selector:
            return mock_pin_btn
        if "确定" in selector:
            return mock_confirm_btn
        m = MagicMock()
        m.count.return_value = 0
        return m

    mock_page.locator.side_effect = locator_side_effect

    res = post_first_comment(mock_page, "测试首评内容", title="测试视频标题", pin=True)
    assert res is True
    mock_page.goto.assert_called_once()
    mock_box.click.assert_called_once()
    mock_page.keyboard.type.assert_called_with("测试首评内容", delay=30)
    mock_btn.click.assert_called_once()
    mock_pin_btn.click.assert_called_once()

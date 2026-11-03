"""回归测试：三处修复（2026-11-04）。

1. kuaishou_reply 真发模式必须排除「点击前已存在的输入框」
   —— 否则会把回复粉丝写成一条普通评论，叠在置顶首评下面。
2. 首评兜底必须用 generate_first_comment（作者视角），不能用
   generate_comment_reply（回复粉丝语义，会产出「你懂啥…」这类对抗口吻）。
3. 首评不得用提问式模板收尾（线上实测 20 条里 12 条是「…吗？」）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bot.ai.copywriter import (
    PLATFORM_PROFILES,
    _check_first_comment,
    _system_prompt,
)
from bot.publish.kuaishou_reply import (
    _FALLBACK_FIRST_COMMENTS,
    generate_first_comment,
)

# ---------- 3. 首评提问式模板拦截 ----------

@pytest.mark.parametrize("text", [
    "已经陷在里面起不来了，周末你们能在床上赖多久？",
    "钻进去直接被封印，如果是你，能在这个窝里躺几天？",
    "为了配这把枫叶伞特意买了长裙，有姐妹和我一样吗？",
    "顶着最雅的屏风打最野的团，你们回家也是这样吗？",
    "左手化妆右手打游戏，给你们这间房能宅几天？",
    "救命太软乎了，大家下班第一件事也是马上换睡衣吗？",
])
def test_question_style_first_comment_blocked(text):
    assert _check_first_comment(text), f"应拦截提问式模板：{text}"


@pytest.mark.parametrize("text", [
    "神龙：我几千年的修为，你拿来烤淀粉肠？",              # 角色台词
    "母鸡裁判刚才叫了一声，你们评评理到底谁踩线了？",      # 喊话求评理
    "兄弟们先别笑了，快帮我看看这算工伤还是蓄意谋杀？",    # 喊话 + 请教
    "唯一的缺点是太容易被封印，今晚谁来把我拔出来？",      # 自嘲式反问
    "小羊：明明是盆先动的手，我叫大声点怎么了！",
    "进门拿坚果上架子一气呵成，房产证写它名字算了",
    "换上这套后遗症太大了，早上闹钟响八遍都起不来！",
    "别夸治愈了，小动物看呆是因为半夜见鬼还开着远光灯",
    "以前手残刮掉半截眉毛的姐妹举个手，让我平衡一下。",
])
def test_good_first_comment_passes(text):
    assert _check_first_comment(text) == [], f"不该被误拦：{text}"


def test_empty_first_comment_passes():
    assert _check_first_comment("") == []


def test_prompt_forbids_question_style():
    """两个快手 profile 的 prompt 都要明确禁止提问式收尾。"""
    for name in ("kuaishou", "kuaishou2"):
        p = _system_prompt(PLATFORM_PROFILES[name])
        assert "严禁把首评写成提问求互动" in p
        assert "了 60%" in p


# ---------- 2. 首评兜底用专用生成器 ----------

def test_generate_first_comment_falls_back_without_key():
    s = MagicMock()
    s.ai_api_key = ""
    with patch("bot.publish.kuaishou_reply.load_settings", return_value=s):
        out = generate_first_comment("唤出万年神龙动静这么大")
    assert out in _FALLBACK_FIRST_COMMENTS


def test_generate_first_comment_empty_title_uses_fallback():
    out = generate_first_comment("")
    assert out in _FALLBACK_FIRST_COMMENTS


def test_fallback_comments_are_not_customer_service_speak():
    """兜底短句不能是「感谢支持」这种客服话术（它会以置顶神评身份出现）。"""
    for t in _FALLBACK_FIRST_COMMENTS:
        assert "感谢" not in t
        assert "关注" not in t
        assert "更精彩" not in t


def test_worker_uses_first_comment_generator_not_reply():
    """publish_worker 的兜底必须调用 generate_first_comment。"""
    import inspect

    import bot.publish_worker as pw

    src = inspect.getsource(pw.process_platform)
    assert "generate_first_comment(" in src
    # 不允许再以「回复粉丝」的语义生成首评
    assert "generate_comment_reply(" not in src


# ---------- 1. 回复输入框排除常驻框 ----------

def test_reply_fill_excludes_preexisting_inputs():
    """fill 阶段的 JS 必须拿到 beforeIds 并据此过滤 inputs。"""
    import inspect

    from bot.publish import kuaishou_reply as kr

    src = inspect.getsource(kr.reply_single_comment_on_page)
    # 第一步要把已存在输入框的标记回传
    assert "beforeIds" in src
    assert "ksReplyMark" in src
    # 第二步要按 before 标记过滤，而不是直接 inputs[inputs.length-1]
    assert "before.has(el.dataset.ksReplyMark" in src
    assert "new Set(beforeIds" in src
    # 不允许再无脑取最后一个可见输入框
    assert "const inputs = [...document.querySelectorAll" not in src

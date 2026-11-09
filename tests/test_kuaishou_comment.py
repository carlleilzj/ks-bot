"""快手作者置顶首评：位置错位、重复发送、假阴性校验。

2026-11-09 线上观测（11-03 起 43 次发布）
----------------------------------------
    准备发表首评   43
    判定已发送     38  (88%)
    成功置顶       37  (86%)
    「未出现在评论区，放弃」 5

那 5 次里，日志显示「作者首评已发送」之后才判失败 —— 说明评论其实发出去了，
只是校验没在页面上找到它。这是**假阴性**，代价是被判「放弃置顶」，
而更危险的后果是重试逻辑误以为没发出去、再点一次发送，留下两条重复首评。

假阴性的两个来源（本文件逐一钉住）：
1. 评论列表渲染慢：固定等 2 秒不够，要用 9 秒轮询。
2. 前 8 字精确匹配太脆：首评含全角标点/emoji/引号时，页面渲染文本与
   输入字符串不完全一致。改成归一化后匹配 + 宽松兜底。
3. 输入框判空用 inner_text()：对 input/textarea 恒为空，会把「读不到」
   误当成「已清空」。
"""

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


# ---------------------------------------------------------------------------
# mock 脚手架
# ---------------------------------------------------------------------------

class _Page:
    """能模拟「评论第 N 次查询才渲染出来」的假页面。"""

    def __init__(self, *, comment_after_queries=0, box_text="",
                 box_input_value="", comment_render_text="测试首评内容"):
        self.comment_queries = 0
        self.comment_after_queries = comment_after_queries
        self.comment_render_text = comment_render_text
        self.goto = MagicMock()
        self.keyboard = MagicMock()
        self.wait_for_timeout = MagicMock()
        self.screenshot = MagicMock()

        box = MagicMock()
        box.count.return_value = 1
        box.is_visible.return_value = True
        box.inner_text.return_value = box_text
        box.input_value.return_value = box_input_value
        box.first = box
        self.box = box

        btn = MagicMock()
        btn.count.return_value = 1
        btn.is_visible.return_value = True
        btn.get_attribute.return_value = "author-comment-input__row__btn"
        btn.first = btn
        self.btn = btn

        title = MagicMock()
        title.count.return_value = 1
        title.inner_text.return_value = "测试视频标题"
        title.first = title
        self.title = title

        pin_btn = MagicMock()
        pin_btn.count.return_value = 1
        pin_btn.is_visible.return_value = True
        pin_btn.first = pin_btn
        self.pin_btn = pin_btn

        confirm = MagicMock()
        confirm.count.return_value = 1
        confirm.is_visible.return_value = True
        confirm.first = confirm
        self.confirm = confirm

        self.locator = MagicMock(side_effect=self._locator)

    def _locator(self, sel):
        if "title" in sel:
            return self.title
        if "author-comment-input__row__input" in sel:
            return self.box
        if "author-comment-input__row__btn" in sel:
            return self.btn
        if "置顶" in sel:
            return self.pin_btn
        if "确定" in sel or "确认" in sel:
            return self.confirm
        if "comment-item" in sel:
            m = MagicMock()
            self.comment_queries += 1
            shown = self.comment_queries > self.comment_after_queries
            item = MagicMock()
            item.count.return_value = 1 if shown else 0
            item.inner_text.return_value = self.comment_render_text if shown else ""
            item.locator.return_value.first = self.pin_btn
            item.first = item
            m.count.return_value = 1 if shown else 0
            m.nth.return_value = item
            m.filter.return_value = item
            return m
        m = MagicMock()
        m.count.return_value = 0
        return m


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------

def test_happy_path_sends_once_and_pins():
    page = _Page()
    res = post_first_comment(page, "测试首评内容", title="测试视频标题", pin=True, settle_sec=0)
    assert res is True
    page.goto.assert_called_once()
    page.box.click.assert_called_once()
    page.keyboard.type.assert_called_with("测试首评内容", delay=30)
    assert page.btn.click.call_count == 1, "只应点一次发送"
    assert page.pin_btn.click.call_count == 1


# ---------------------------------------------------------------------------
# 假阴性 1：评论列表渲染慢
# ---------------------------------------------------------------------------

def test_waits_for_slow_render_instead_of_giving_up():
    """评论要查 4 次（约 2 秒）才出现 —— 必须继续等，不能判失败。"""
    page = _Page(comment_after_queries=4)
    res = post_first_comment(page, "测试首评内容", title="测试视频标题", pin=True, settle_sec=0)
    assert res is True
    assert page.btn.click.call_count == 1, "渲染慢绝不能触发重发"
    assert page.pin_btn.click.call_count == 1


def test_does_not_click_send_again_when_box_cleared():
    """输入框已清空 = 上一次真的发出去了，绝不能再点发送。

    回归 2026-11-09 的线上现象：判失败后重试，评论区出现两条一样的首评。
    """
    # inner_text 空、input_value 也空 → 判定已清空
    page = _Page(comment_after_queries=6, box_text="", box_input_value="")
    res = post_first_comment(page, "测试首评内容", title="测试视频标题", pin=False, settle_sec=0)
    assert res is True
    assert page.btn.click.call_count == 1, "输入框已清空时不得重复发送"


def test_box_with_text_is_not_treated_as_cleared():
    """输入框仍有文本 = 真的没发出去，允许重试。

    inner_text 有值但 input_value 为空时（真实 input 元素），
    不能因为 input_value 读不到就误判成「已清空」。
    """
    page = _Page(comment_after_queries=999, box_text="测试首评内容",
                 box_input_value="")
    post_first_comment(page, "测试首评内容", title="测试视频标题", pin=False, settle_sec=0)
    assert page.btn.click.call_count >= 2, "输入框还有内容时应重试发送"


def test_gives_up_as_sent_when_box_cleared_but_list_never_renders():
    """三次都查不到，但输入框已清空 → 判已发送，且绝不重复点发送。

    线上真实场景（11-04 15:50 / 11-05 13:30 / 11-08 20:55 / 11-09 12:08、14:13）：
    作品刚发布、管理页列表还没同步，评论区渲染也慢，三次查询都不到，
    日志判「放弃置顶」。而输入框其实是空的 —— 说明评论已经发出去了。
    此时判失败会让置顶被跳过；更糟的是下次触发重试会造成重复首评。
    """
    page = _Page(comment_after_queries=999, box_text="", box_input_value="")
    res = post_first_comment(page, "测试首评内容", title="测试视频标题", pin=False, settle_sec=0)
    assert res is True, "输入框已清空时宁可判已发送，也不能判失败"
    assert page.btn.click.call_count == 1, "绝不允许重复发送"


def test_does_not_falsely_succeed_when_box_still_has_text():
    """输入框仍有内容且列表查不到 = 真的没发出去，必须返回 False。"""
    page = _Page(comment_after_queries=999, box_text="测试首评内容",
                 box_input_value="测试首评内容")
    res = post_first_comment(page, "测试首评内容", title="测试视频标题", pin=False, settle_sec=0)
    assert res is False


# ---------------------------------------------------------------------------
# 假阴性 2：前 8 字匹配太脆
# ---------------------------------------------------------------------------

def test_matches_comment_despite_punctuation_differences():
    """页面把全角标点渲染成半角时，仍应认出是自己的首评。

    真实首评形如「兄弟们先别笑了，快帮我看看这算工伤还是蓄意谋杀？」
    页面上可能显示成半角逗号，前 8 字精确匹配会失败。
    """
    page = _Page(comment_render_text="兄弟们先别笑了,快帮我看看这算工伤还是蓄意谋杀?")
    res = post_first_comment(
        page, "兄弟们先别笑了，快帮我看看这算工伤还是蓄意谋杀？",
        title="测试视频标题", pin=True)
    assert res is True
    assert page.btn.click.call_count == 1
    assert page.pin_btn.click.call_count == 1, "归一化匹配成功后应能置顶"


def test_matches_comment_with_emoji_noise():
    """评论项里带作者昵称/时间等噪声时也能匹配到正文。"""
    page = _Page(comment_render_text="作者 刚刚 换上新床单那刻真的被治愈了")
    res = post_first_comment(
        page, "换上新床单那刻真的被治愈了", title="测试视频标题", pin=False)
    assert res is True
    assert page.btn.click.call_count == 1


# ---------------------------------------------------------------------------
# 防错位：不能把首评发到别的作品
# ---------------------------------------------------------------------------

def test_skips_when_target_video_not_matched():
    """页面选中的不是目标作品时必须放弃（避免把首评发到上一条）。"""
    page = _Page()
    page.title.inner_text.return_value = "完全不相干的另一个标题"
    # 没有「选择视频」抽屉可点
    res = post_first_comment(page, "测试首评内容", title="测试视频标题", pin=False, settle_sec=0)
    assert res is False
    assert page.keyboard.type.call_count == 0


# ---------------------------------------------------------------------------
# 发布后等待作品同步（settle）
# ---------------------------------------------------------------------------

def test_settles_before_touching_comment_page():
    """必须先等作品同步进列表，再打开评论管理页找它。

    回归 2026-11-09：发布接口回包秒回 photoId，首评在发布后 0~2 秒就
    开始找作品，而平台侧同步要 30~90 秒，于是日志反复出现
    「新作品尚未同步到列表」→「未出现在评论区」→「放弃置顶」。
    """
    page = _Page()
    calls = []
    page.wait_for_timeout = MagicMock(side_effect=lambda ms: calls.append(ms))

    post_first_comment(page, "测试首评内容", title="测试视频标题",
                       pin=False, settle_sec=45)

    # 第一次等待必须是 settle（45 秒 = 45000ms），且发生在 goto 之前
    assert calls and calls[0] == 45000, f"首次等待应为 settle，实际 {calls[:3]}"

    # 用一个只记录顺序的版本来验证 settle 早于页面跳转
    order = []
    page2 = _Page()
    page2.wait_for_timeout = MagicMock(side_effect=lambda ms: order.append(("wait", ms)))
    page2.goto = MagicMock(side_effect=lambda *a, **k: order.append(("goto", 0)))
    post_first_comment(page2, "测试首评内容", title="测试视频标题",
                       pin=False, settle_sec=10)
    kinds = [k for k, _ in order]
    assert kinds.index("wait") < kinds.index("goto"), \
        f"settle 必须在打开评论管理页之前，实际顺序 {kinds[:5]}"


def test_settle_zero_skips_waiting():
    """settle_sec=0 时不得插入等待（测试与重试场景用）。"""
    page = _Page()
    calls = []
    page.wait_for_timeout = MagicMock(side_effect=lambda ms: calls.append(ms))
    post_first_comment(page, "测试首评内容", title="测试视频标题",
                       pin=False, settle_sec=0)
    assert 45000 not in calls

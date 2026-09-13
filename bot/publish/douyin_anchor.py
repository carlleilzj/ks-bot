"""抖音发布页「添加标签」挂载能力（v2，实测校准）。

发布页结构（2026-09 实测，creator.douyin.com/creator-micro/content/upload）：

    扩展信息
      添加标签
        <semi-select  #N>   ← 挂载类型下拉，选项：
            位置       → 「输入地理位置」semi-select
            团购       → 「全国 / 请选择团购商品」
            影视演艺    → 「请选择」
            小程序      → 「粘贴抖音小程序链接」
            游戏手柄    → 「添加作品同款游戏」
            标记万物    → 「请输入或选择标记的物品」INPUT
        关联热点  → <semi-select placeholder「点击输入热点词」>

关键：**「关联热点」本身也是一个 semi-select**，不是普通 input。
所有 semi-select-selection-text 列表：
    [0] 合集 / [1] 请选择合集 / [2] 位置(挂载类型) / [3] 带货模式 /
    [4] 输入地理位置 / [5] 点击输入热点词
"""
from __future__ import annotations

import logging
import time

from playwright.sync_api import Page

log = logging.getLogger("douyin_anchor")

# 挂载类型 -> 选中后出现的目标：("ph", 值) 表示 placeholder,  "select" 表示 semi-select
ANCHOR_TARGETS = {
    "位置": ("ph", "输入地理位置"),
    "团购": ("ph", "请选择团购商品"),
    "影视演艺": ("ph", "请选择"),
    "小程序": ("ph", "粘贴抖音小程序链接"),
    "游戏手柄": ("ph", "添加作品同款游戏"),
    "标记万物": ("ph", "请输入或选择标记的物品"),
}

HOT_TOPIC_PLACEHOLDER = "点击输入热点词"
HINT_PLACEHOLDER = "输入地理位置"   # 挂在类型下拉旁的提示项，用于识别正确索引


def _sel_texts(page: Page) -> list[str]:
    sel = page.locator("span.semi-select-selection-text")
    out = []
    for i in range(sel.count()):
        try:
            out.append((sel.nth(i).inner_text() or "").strip())
        except Exception:
            out.append("")
    return out


def _scroll_to_tags(page: Page) -> None:
    for _ in range(9):
        page.mouse.wheel(0, 700)
        page.wait_for_timeout(450)
    page.wait_for_timeout(1500)


def _anchor_type_dropdown(page: Page):
    """定位「添加标签」的挂载类型下拉。

    实测它紧挨在提示项「输入地理位置」之前，或当前值已是某个类型名。
    """
    sel = page.locator("span.semi-select-selection-text")
    texts = _sel_texts(page)
    # a) 当前值就是某个类型名（挂载过之后会显示出来）
    for i, t in enumerate(texts):
        if t in ANCHOR_TARGETS:
            return sel.nth(i)
    # b) 找提示项「输入地理位置」，它前一个就是类型下拉
    for i, t in enumerate(texts):
        if t == HINT_PLACEHOLDER and i > 0:
            return sel.nth(i - 1)
    # c) 找「带货模式」（默认值）
    for i, t in enumerate(texts):
        if t == "带货模式":
            return sel.nth(i)
    return None


def _pick_dropdown_option(page: Page, text: str, timeout_ms: int = 3000) -> bool:
    """在展开的下拉里点选包含 text 的选项。"""
    page.wait_for_timeout(timeout_ms)
    for sel in ("[class*='select-dropdown-option']:visible",
                "[class*='dropdown'] [class*='option']:visible",
                "[role='option']:visible"):
        try:
            opt = page.locator(sel, has_text=text)
            if opt.count():
                opt.first.click()
                return True
        except Exception:
            continue
    return False


def apply_anchor(page: Page, anchor_type: str, value: str) -> bool:
    """挂载单个标签。返回是否成功。"""
    if anchor_type not in ANCHOR_TARGETS:
        log.warning("未知挂载类型 %r（可选：%s）", anchor_type, "/".join(ANCHOR_TARGETS))
        return False

    _scroll_to_tags(page)
    dd = _anchor_type_dropdown(page)
    if dd is None:
        log.warning("未找到「添加标签」下拉（页面可能已改版）")
        return False

    # 1. 开下拉选类型
    try:
        dd.click()
        page.wait_for_timeout(2000)
    except Exception as e:
        log.warning("打开挂载下拉失败：%s", str(e)[:100])
        return False

    if not _pick_dropdown_option(page, anchor_type, 1800):
        log.warning("下拉中无 %r 选项（账号可能未开通该权限）", anchor_type)
        page.keyboard.press("Escape")
        return False
    page.wait_for_timeout(3500)

    # 2. 填值
    kind, target = ANCHOR_TARGETS[anchor_type]
    try:
        if kind == "ph":
            inp = page.get_by_placeholder(target)
            if not inp.count():
                inp = page.locator(f"input[placeholder*='{target[:5]}']")
            if not inp.count():
                log.warning("%r 选中后未出现输入框（placeholder=%r）", anchor_type, target)
                return False
            inp.first.click()
            page.wait_for_timeout(600)
            inp.first.fill(value)
            page.wait_for_timeout(2800)
            _pick_dropdown_option(page, value, 500)
        else:
            log.warning("%r 的输入方式未实现", anchor_type)
            return False
        log.info("已挂载 %s：%s", anchor_type, str(value)[:40])
        return True
    except Exception as e:
        log.warning("填写挂载值失败（%s=%r）：%s", anchor_type, str(value)[:30], str(e)[:100])
        return False


def apply_hot_topic(page: Page, keyword: str) -> bool:
    """关联热点：点开 semi-select（placeholder「点击输入热点词」）→ 搜词 → 选第一项。"""
    if not keyword:
        return False
    try:
        _scroll_to_tags(page)
        sel = page.locator("span.semi-select-selection-text")
        target = None
        for i in range(sel.count()):
            try:
                t = (sel.nth(i).inner_text() or "").strip()
                if t == HOT_TOPIC_PLACEHOLDER:
                    target = sel.nth(i)
                    break
            except Exception:
                continue
        if target is None:
            log.warning("未找到热点 semi-select（placeholder=%r）", HOT_TOPIC_PLACEHOLDER)
            return False

        target.click()
        page.wait_for_timeout(2500)

        # 展开后有搜索输入框
        typed = False
        for ph in ("搜索", "输入", "关键词", "热点词"):
            inp = page.locator(f"input[placeholder*='{ph}']:visible")
            if inp.count():
                inp.first.fill(keyword)
                typed = True
                break
        if not typed:
            # 有些版本展开后直接可键盘输入
            page.keyboard.type(keyword, delay=40)
        page.wait_for_timeout(3000)

        if _pick_dropdown_option(page, keyword, 500):
            log.info("已关联热点：%s", keyword[:30])
            return True
        # 兜底：点第一个可见候选
        for sel2 in ("[class*='dropdown'] [class*='option']:visible",
                     "[role='option']:visible"):
            opts = page.locator(sel2)
            if opts.count():
                opts.first.click()
                log.info("已关联热点（首个候选）：%s", keyword[:30])
                return True
        log.warning("热点 %r 无候选项", keyword[:30])
        page.keyboard.press("Escape")
        return False
    except Exception as e:
        log.warning("关联热点失败：%s", str(e)[:120])
        return False


def apply_anchors(page: Page, anchors: dict | None, hot_topic: str = "") -> list[str]:
    """批量挂载。anchors: {类型: 值}。返回成功的项（类型名或 '热点'）。"""
    done: list[str] = []
    if anchors:
        for atype, val in anchors.items():
            if not val:
                continue
            try:
                if apply_anchor(page, atype, str(val)):
                    done.append(atype)
            except Exception as e:
                log.warning("挂载 %s 异常：%s", atype, str(e)[:100])
            time.sleep(1)
    if hot_topic:
        try:
            if apply_hot_topic(page, hot_topic):
                done.append("热点")
        except Exception as e:
            log.warning("挂载热点异常：%s", str(e)[:100])
    return done


def pick_goods_for(text: str = "") -> str:
    """根据视频文本推荐带货关键词（标记万物）。"""
    lower = (text or "").lower()
    if any(k in lower for k in ("狗", "猫", "宠", "动物", "pet", "dog", "cat", "puppy", "kitten")):
        return "狗粮"
    if any(k in lower for k in ("洗", "洁", "净", "刷", "拖", "收纳")):
        return "清洁用品"
    return "生活日用"

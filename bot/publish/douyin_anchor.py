"""抖音发布页「添加标签」挂载能力（v3，2026-09-22 改版校准）。

2026-09-22 发布页截图：扩展信息不再有「位置 / 标记万物 / 带货模式」二级下拉，
「添加标签」变成搜索框，输入后直接出话题/商品候选（含全国可挂的日用品）。
旧 v2 路径（semi-select 选类型 → 二级下拉填值）每天都报
「标记万物选中后未出现二级下拉」，只热点挂得上。

新路径：在「添加标签」搜索框输入全国商品关键词 → 点第一个商品候选。
旧路径保留作兜底（账号若还露出类型下拉仍能走）。
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
    "商品": ("ph", "请选择团购商品"),   # 新版搜索框别名，全国商品推广
    "影视演艺": ("ph", "请选择"),
    "小程序": ("ph", "粘贴抖音小程序链接"),
    "游戏手柄": ("ph", "添加作品同款游戏"),
    "标记万物": ("ph", "请输入或选择标记的物品"),
}

# 走搜索框、不走类型下拉的类型（2026-09-22 新版「添加标签」）
SEARCH_ANCHOR_TYPES = ("商品", "团购", "标记万物")

# 全国可挂的日用商品（不绑门店）。抽纸搜索结果稳定、全平台有货。
DEFAULT_NATIONWIDE_GOODS = "抽纸"

HOT_TOPIC_PLACEHOLDER = "点击输入热点词"
HOT_TOPIC_PLACEHOLDERS = (
    "点击输入热点词",
    "关联热点，有机会享现金和流量激励",
    "关联热点",
)
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


def _verified(page: Page, anchor_type: str, value: str) -> bool:
    """回读表单确认挂载真的生效。

    判据：
      - 类型栏（index 2 附近）已显示 anchor_type，不再是「带货模式」占位
      - 值栏出现了 value（城市名），或至少不再是 placeholder 文案
    只信这个，不信点击动作的返回值。
    """
    texts = _sel_texts(page)
    if anchor_type not in texts:
        return False
    kind, target = ANCHOR_TARGETS.get(anchor_type, ("", ""))
    if not target:
        return False
    # 值栏应从 placeholder 变成真实值
    if target in texts:
        # 仍是 placeholder → 没填上
        return False
    return bool(value) and any(value in t for t in texts)


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


def open_semi_select(locator, page: Page, settle_ms: int = 2000) -> bool:
    """展开一个 semi-design 下拉。

    2026-09-17 实测：semi-select 的 `span.semi-select-selection-text` 是纯文本
    span，Playwright 的 `click()` 会因为「元素不可点击」等到 timeout（30s！），
    而 `mousedown/mouseup/click` 三连派发能可靠展开。

    这与快手星火用的 antd Select 是同一个坑（那边也是靠 mousedown 展开）。
    抖音用 semi-design，事件语义相同。
    """
    try:
        # 优先在真实 .semi-select 容器上派发（更接近用户操作）
        locator.evaluate("""el => {
            const box = el.closest('.semi-select') || el.parentElement || el;
            for (const t of ['mousedown', 'mouseup', 'click']) {
                box.dispatchEvent(new MouseEvent(t, {
                    bubbles: true, cancelable: true, view: window, detail: 1
                }));
            }
        }""")
        page.wait_for_timeout(settle_ms)
        return True
    except Exception as e:
        log.warning("派发 mousedown 展开下拉失败：%s", str(e)[:100])
        return False


def _visible_options(page: Page) -> list[str]:
    """回读当前可见的下拉选项文本。

    2026-09-17 实测 DOM 结构（semi-design v2）：
        <div class="semi-select-option-list" role="listbox">
          <div class="semi-select-option" role="option">
            <div class="semi-select-option-icon">…tick svg…</div>
            <div class="semi-select-option-text">带货模式</div>   ← 真正的文案在这
          </div>
        </div>

    注意：页面里还有个 `div.select-dropdown-option-video`（挂载类型下拉的
    触发器预留节点，rect 就是按钮本身），**不是选项**，必须排除——旧代码
    用 [class*='select-dropdown-option'] 匹配到的正是这个，导致点了个假节点。
    """
    try:
        return page.evaluate("""() => {
            const out = [];
            for (const o of document.querySelectorAll(
                    ".semi-select-option, .semi-select-option-list [role='option']")) {
                if (!(o.offsetParent || o.getClientRects().length)) continue;
                const t = o.querySelector(".semi-select-option-text") || o;
                const s = (t.textContent || '').replace(/\\s+/g, ' ').trim();
                if (s && !out.includes(s)) out.push(s);
            }
            return out;
        }""") or []
    except Exception:
        return []


def _click_option(page: Page, want: str) -> bool:
    """点选 semi 下拉里文案等于 want 的选项（在真实 option 节点上派发鼠标事件）。"""
    try:
        return bool(page.evaluate("""(want) => {
            const opts = [...document.querySelectorAll(".semi-select-option")];
            // 精确匹配优先，其次包含
            let hit = opts.find(o => {
                const t = (o.querySelector('.semi-select-option-text') || o);
                return (t.textContent || '').replace(/\\s+/g, ' ').trim() === want;
            });
            if (!hit) hit = opts.find(o => {
                const t = (o.querySelector('.semi-select-option-text') || o);
                return (t.textContent || '').replace(/\\s+/g, ' ').trim().includes(want);
            });
            if (!hit) return false;
            // 派发到最内层有文本的节点，模拟真实点击
            const target = hit.querySelector('.semi-select-option-text') || hit;
            for (const node of [target, hit]) {
                for (const ev of ['mousedown', 'mouseup', 'click']) {
                    node.dispatchEvent(new MouseEvent(ev, {
                        bubbles: true, cancelable: true, view: window, detail: 1
                    }));
                }
            }
            return true;
        }""", want))
    except Exception as e:
        log.warning("点选选项 %r 异常：%s", want, str(e)[:80])
        return False


def _pick_dropdown_option(page: Page, text: str, timeout_ms: int = 3000,
                          verify=None) -> bool:
    """在展开的下拉里点选包含 text 的选项。

    点完可选调用 verify() 回读确认——semi 的下拉有时会「点了但没选中」，
    只信点击动作会造成假成功（实测踩过）。
    """
    page.wait_for_timeout(timeout_ms)
    deadline = time.time() + 6
    while time.time() < deadline:
        opts = _visible_options(page)
        if opts:
            hit = next((o for o in opts if o == text), None) or \
                  next((o for o in opts if text in o), None)
            if hit is not None and _click_option(page, hit):
                if verify is None:
                    return True
                page.wait_for_timeout(900)
                if verify():
                    return True
                log.warning("点选 %r 后回读未确认，重试", hit[:20])
        page.wait_for_timeout(400)
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

    # 1. 开下拉选类型（semi-select 需派发 mousedown，click() 会超时 30s）
    if not open_semi_select(dd, page):
        log.warning("打开挂载下拉失败（mousedown 派发异常）")
        return False

    if not _pick_dropdown_option(page, anchor_type, 1800,
                                 verify=lambda: anchor_type in _sel_texts(page)):
        log.warning("下拉中无 %r 选项（账号可能未开通该权限）", anchor_type)
        page.keyboard.press("Escape")
        return False
    page.wait_for_timeout(3500)
    if anchor_type not in _sel_texts(page):
        log.warning("选中 %r 后类型栏未回读确认", anchor_type)
        return False

    # 2. 填值
    # 2026-09-17 实测：选中「位置」后出现的是**第二个 semi-select**
    # （文本「输入地理位置」），不是 input。旧实现 get_by_placeholder 恒为 0 命中。
    # 正确路径：展开这个二级 semi-select → 键盘输入关键词 → 在候选中点选。
    kind, target = ANCHOR_TARGETS[anchor_type]
    try:
        if kind != "ph":
            log.warning("%r 的输入方式未实现", anchor_type)
            return False

        # 找二级 semi-select（文本 == target）
        sels = page.locator("span.semi-select-selection-text")
        sub = None
        for i in range(sels.count()):
            try:
                if (sels.nth(i).inner_text() or "").strip() == target:
                    sub = sels.nth(i)
                    break
            except Exception:
                continue
        if sub is None:
            log.warning("%r 选中后未出现二级下拉（文本=%r）", anchor_type, target)
            return False

        if not open_semi_select(sub, page, settle_ms=2500):
            log.warning("%r 二级下拉展开失败", anchor_type)
            return False

        # 展开后列表自带热门候选；直接键盘输入可过滤（无独立 input 元素）
        page.keyboard.type(str(value), delay=60)
        page.wait_for_timeout(3000)

        if _pick_dropdown_option(page, str(value), 800,
                                 verify=lambda: _verified(page, anchor_type, str(value))):
            log.info("已挂载 %s：%s（已回读校验）", anchor_type, str(value)[:40])
            return True
        # 兜底：候选里第一个不像类型名/占位的真实结果
        opts = [o for o in _visible_options(page)
                if o not in ANCHOR_TARGETS and len(o) > 2]
        if opts and _pick_dropdown_option(page, opts[0], 300,
                                          verify=lambda: _verified(page, anchor_type, str(value))):
            log.info("已挂载 %s（首个候选）：%r", anchor_type, opts[0][:30])
            return True
        log.warning("%r 未能挂载（关键词=%r，回读未确认）", anchor_type, str(value)[:20])
        page.keyboard.press("Escape")
        return False
    except Exception as e:
        log.warning("填写挂载值失败（%s=%r）：%s", anchor_type, str(value)[:30], str(e)[:100])
        return False


def apply_tag_search(page: Page, keyword: str) -> bool:
    """新版「添加标签」搜索框：输入全国商品关键词，点第一个商品候选。

    2026-09-22 截图：下拉里混着话题和商品（「小林制药即贴暖宝宝  身体护理」）。
    优先点含「护理/日用/纸/清洁/食品」的商品行，否则点第一个非纯分类词。
    """
    if not keyword:
        return False
    try:
        _scroll_to_tags(page)
        inp = _find_tag_search_input(page)
        if inp is None:
            log.warning("未找到「添加标签」搜索框（页面可能还是旧版类型下拉）")
            return False
        try:
            inp.click(timeout=2000)
        except Exception:
            pass
        try:
            inp.fill("")
            inp.fill(str(keyword))
        except Exception:
            page.keyboard.type(str(keyword), delay=50)
        page.wait_for_timeout(2500)

        opts = _visible_options(page) or _visible_search_rows(page)
        if not opts:
            log.warning("添加标签搜索 %r 无候选项", keyword[:20])
            page.keyboard.press("Escape")
            return False

        product_hints = ("护理", "日用", "纸", "清洁", "食品", "零食", "家居",
                         "厨房", "洗护", "母婴", "数码", "百货")
        hit = next((o for o in opts if any(h in o for h in product_hints)), None)
        if hit is None:
            hit = next((o for o in opts if keyword in o), None)
        if hit is None:
            skip = {"社会科学", "自然科学", "添加标签", "关联热点", "视频章节"}
            hit = next((o for o in opts if o not in skip and len(o) > 1), opts[0])

        if not _click_option(page, hit):
            # 搜索结果不一定是 semi-select-option，按可见文本再点一次
            if not _click_visible_text(page, hit):
                log.warning("添加标签候选 %r 点不到", hit[:30])
                page.keyboard.press("Escape")
                return False
        page.wait_for_timeout(900)
        if _tag_search_verified(page, keyword, hit):
            log.info("已挂全国商品（搜索框）：%s → %s", keyword[:20], hit[:40])
            return True
        log.warning("添加标签点选 %r 后回读未确认", hit[:30])
        return False
    except Exception as e:
        log.warning("添加标签搜索失败：%s", str(e)[:120])
        return False


def _find_tag_search_input(page: Page):
    """定位「添加标签」旁边的搜索/输入框。"""
    # 1) placeholder 含 标签/搜索/商品
    for ph in ("搜索标签", "添加标签", "搜索商品", "输入标签", "搜索"):
        loc = page.locator(f"input[placeholder*='{ph}']:visible")
        if loc.count():
            return loc.first
    # 2) 文案「添加标签」右侧最近的可见 input
    try:
        handle = page.evaluate_handle("""() => {
            const labels = [...document.querySelectorAll('*')].filter(
                e => (e.childNodes.length && [...e.childNodes].some(
                    n => n.nodeType === 3 && (n.textContent || '').trim() === '添加标签')));
            const lab = labels[0];
            if (!lab) return null;
            const root = lab.closest('div') || lab.parentElement;
            const inp = (root && root.querySelector('input'))
                     || lab.parentElement?.querySelector('input');
            return inp || null;
        }""")
        el = handle.as_element() if handle else None
        if el:
            return el
    except Exception:
        pass
    # 3) 扩展信息区域第一个可见 input
    loc = page.locator("input:visible")
    n = min(loc.count(), 8)
    for i in range(n):
        try:
            ph = (loc.nth(i).get_attribute("placeholder") or "")
            if "热点" in ph or "章节" in ph or "标题" in ph:
                continue
            return loc.nth(i)
        except Exception:
            continue
    return None


def _visible_search_rows(page: Page) -> list[str]:
    """搜索下拉不一定是 semi-select-option，再扫一遍可见列表行。"""
    try:
        return page.evaluate("""() => {
            const out = [];
            const nodes = document.querySelectorAll(
                "[role='option'], [class*='option'], [class*='suggest'], [class*='search-item']");
            for (const o of nodes) {
                if (!(o.offsetParent || o.getClientRects().length)) continue;
                const s = (o.textContent || '').replace(/\\s+/g, ' ').trim();
                if (s && s.length < 40 && !out.includes(s)) out.push(s);
            }
            return out;
        }""") or []
    except Exception:
        return []


def _click_visible_text(page: Page, text: str) -> bool:
    try:
        loc = page.get_by_text(text, exact=False)
        if loc.count():
            loc.first.click(timeout=1500)
            return True
    except Exception:
        return False
    return False


def _tag_search_verified(page: Page, keyword: str, picked: str) -> bool:
    """搜索挂载回读：页面出现关键词或所选商品名。"""
    needles = [keyword, picked[:8]]
    try:
        body = page.locator("body").inner_text(timeout=3000)
    except Exception:
        body = ""
    texts = " ".join(_sel_texts(page) + [body[:2000]])
    return any(n and n in texts for n in needles)


def apply_hot_topic(page: Page, keyword: str) -> bool:
    """关联热点：点开 semi-select → 搜词 → 选第一项。"""
    if not keyword:
        return False
    try:
        _scroll_to_tags(page)
        sel = page.locator("span.semi-select-selection-text")
        target = None
        for i in range(sel.count()):
            try:
                t = (sel.nth(i).inner_text() or "").strip()
                if t in HOT_TOPIC_PLACEHOLDERS or HOT_TOPIC_PLACEHOLDER in t or t.startswith("关联热点"):
                    target = sel.nth(i)
                    break
            except Exception:
                continue
        if target is None:
            log.warning("未找到热点 semi-select（placeholder=%r）", HOT_TOPIC_PLACEHOLDERS)
            return False

        # 展开热点 semi-select（同样需 mousedown）
        if not open_semi_select(target, page, settle_ms=2500):
            log.warning("打开热点下拉失败")
            return False

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

        # 回读判据：热点栏不再是空占位（新旧两版文案都算空）
        def _hot_ok() -> bool:
            texts = _sel_texts(page)
            return not any(
                t in HOT_TOPIC_PLACEHOLDERS or t.startswith("关联热点")
                for t in texts
            )

        if _pick_dropdown_option(page, keyword, 500, verify=_hot_ok):
            log.info("已关联热点：%s（已回读校验）", keyword[:30])
            return True
        # 兜底：点第一个可见候选
        opts = _visible_options(page)
        if opts:
            if _pick_dropdown_option(page, opts[0], 200, verify=_hot_ok):
                log.info("已关联热点（首个候选）：%s → %r", keyword[:30], opts[0][:30])
                return True
        log.warning("热点 %r 无候选项", keyword[:30])
        page.keyboard.press("Escape")
        return False
    except Exception as e:
        log.warning("关联热点失败：%s", str(e)[:120])
        return False


def apply_anchors(page: Page, anchors: dict | None, hot_topic: str = "") -> list[str]:
    """批量挂载。anchors: {类型: 值}。返回成功的项（类型名或 '热点'）。

    商品/团购/标记万物优先走新版搜索框（全国商品）；失败再走旧类型下拉。
    """
    done: list[str] = []
    if anchors:
        for atype, val in anchors.items():
            if not val:
                continue
            v = DEFAULT_NATIONWIDE_GOODS if str(val).lower() in ("auto", "全国") else str(val)
            try:
                ok = False
                if atype in SEARCH_ANCHOR_TYPES:
                    ok = apply_tag_search(page, v)
                if not ok:
                    ok = apply_anchor(page, atype, v)
                if ok:
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
    """全国可挂的日用商品关键词。宠物向仍用狗粮，其余默认抽纸。"""
    lower = (text or "").lower()
    if any(k in lower for k in ("狗", "猫", "宠", "动物", "pet", "dog", "cat", "puppy", "kitten")):
        return "狗粮"
    if any(k in lower for k in ("洗", "洁", "净", "刷", "拖", "收纳")):
        return "抽纸"
    return DEFAULT_NATIONWIDE_GOODS

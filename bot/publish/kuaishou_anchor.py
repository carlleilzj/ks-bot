"""快手发布页挂载能力 v3：作者服务（全类型）+ 热点 + 地点 + 作者声明。

实测结构（2026-09，cp.kuaishou.com/article/publish/video）——发布页 6 个 ant-select：

    [0] 选择服务类型             作者服务，一级下拉 3 项：
                                    关联推广任务（磁力聚星）
                                    关联变现任务（星火）
                                    关联小程序（CPA）
                                  选中后出现二级下拉「关联成功可获得更多收益」
    [1] 关联成功可获得更多收益     二级下拉（任务/小程序列表）
    [2] 输入你想关联的热点        热点
    [3] 为作品添加补充说明        作者声明
    [4] 请选择所在地区            地点（ant-cascader 级联）
    [5] 请输入视频详细地址        详细地址

配置（config.yaml platforms.kuaishou）：
    spark_task: true                     # = 关联变现任务
    anchors:
      热点: "搞笑"
      地点: "郴州"
      推广任务: "任务关键词"             # 关联推广任务（磁力聚星）
      小程序: "小程序关键词"             # 关联小程序（CPA）
      声明: "内容由AI生成"                # 作者声明

失败一律只 warning，不阻塞发布。
"""
from __future__ import annotations

import logging
import time

from playwright.sync_api import Page

from .base import shot

log = logging.getLogger(__name__)

# 作者服务一级选项 → 配置键
SERVICE_MAP = {
    "关联商品": "商品",
    "关联推广任务": "推广任务",   # 磁力聚星
    "关联变现任务": "变现任务",   # 星火
    "关联小程序": "小程序",       # CPA，选中后需粘贴小程序链接（非下拉）
}

# 各服务选中后，二级下拉的 placeholder（实测：不同服务文案不同！）
SERVICE_SUB_PLACEHOLDER = {
    "关联商品": "关联商品获得更多收入",
    "关联推广任务": "关联成功可获得更多收益",
    "关联变现任务": "关联成功可获得更多收益",
}
# 兜底：任何含「关联」「获得更多收入」「收益」的 placeholder
SUB_PLACEHOLDER_HINTS = ("关联商品获得更多收入", "关联成功可获得更多收益",
                         "获得更多收入", "更多收益")


def _utils():
    from . import kuaishou as ks
    return ks


def kill_overlays(page: Page) -> None:
    """关掉遮挡点击的 cp-dialog 弹窗与 joyride 遮罩。"""
    ks = _utils()
    for fn in (ks.dismiss_dialogs, ks._remove_joyride):
        try:
            fn(page)
        except Exception:
            pass
    for _ in range(3):
        try:
            if not page.locator(".cp-dialog-wrapper:visible").count():
                break
            closed = False
            for sel in (".cp-dialog-close", "[class*='dialog'] [class*='close']",
                        "button:has-text('知道了')", "button:has-text('我知道了')"):
                c = page.locator(sel + ":visible")
                if c.count():
                    try:
                        c.first.click(timeout=2000)
                        page.wait_for_timeout(1000)
                        closed = True
                        break
                    except Exception:
                        continue
            if not closed:
                page.keyboard.press("Escape")
                page.wait_for_timeout(1000)
        except Exception:
            break


# ---------- ant-select 基础操作 ----------

def _select_by_placeholder(page: Page, placeholder: str):
    """按 placeholder 文本找 .ant-select 容器。"""
    sel = page.locator(".ant-select")
    for i in range(sel.count()):
        try:
            el = sel.nth(i)
            ph = ""
            try:
                ph = el.locator(".ant-select-selection-placeholder").inner_text(timeout=800)
            except Exception:
                pass
            if placeholder in (ph or ""):
                return el
        except Exception:
            continue
    return None


def _open_select(page: Page, el) -> bool:
    try:
        el.scroll_into_view_if_needed(timeout=3000)
        page.wait_for_timeout(400)
    except Exception:
        pass
    for attempt in range(2):
        try:
            el.click(timeout=4000, force=(attempt == 1))
            page.wait_for_timeout(1800)
            return True
        except Exception:
            if attempt == 1:
                return False
    return False


def _visible_options(page: Page) -> list[str]:
    out: list[str] = []
    for sel in (".ant-select-item-option-content", ".ant-select-item-option",
                ".ant-cascader-menu-item"):
        try:
            loc = page.locator(f"{sel}:visible")
            for i in range(loc.count()):
                t = (loc.nth(i).inner_text() or "").strip()
                if t and t not in out:
                    out.append(t)
        except Exception:
            continue
    return out


def _click_option(page: Page, text: str) -> bool:
    for sel in (".ant-select-item-option-content", ".ant-select-item-option",
                ".ant-cascader-menu-item"):
        try:
            loc = page.locator(f"{sel}:visible", has_text=text)
            if loc.count():
                loc.first.click(timeout=3000)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


def _read_selected(page: Page, el) -> str:
    for sel in (".ant-select-selection-item", ".ant-select-selection-placeholder"):
        try:
            t = el.locator(sel).inner_text(timeout=800)
            if t and t.strip():
                return t.strip()
        except Exception:
            continue
    return ""


def _clear_select(page: Page, el) -> None:
    try:
        c = el.locator(".ant-select-clear")
        if c.count():
            c.first.click()
            page.wait_for_timeout(1000)
    except Exception:
        pass


# ---------- 作者服务（推广任务 / 变现任务 / 小程序） ----------

def _find_sub_select(page: Page, service_name: str):
    """按服务类型找它的二级下拉（各服务 placeholder 文案不同）。"""
    want = SERVICE_SUB_PLACEHOLDER.get(service_name, "")
    if want:
        el = _select_by_placeholder(page, want)
        if el is not None:
            return el
    # 兜底 1：按 hints 逐个试
    for hint in SUB_PLACEHOLDER_HINTS:
        el = _select_by_placeholder(page, hint)
        if el is not None:
            return el
    # 兜底 2：扫所有 ant-select，挑 placeholder 含「关联/收益」的
    sel = page.locator(".ant-select")
    for i in range(sel.count()):
        try:
            el = sel.nth(i)
            ph = ""
            try:
                ph = el.locator(".ant-select-selection-placeholder").inner_text(timeout=600)
            except Exception:
                pass
            if ph and ("关联" in ph or "收益" in ph or "收入" in ph):
                return el
        except Exception:
            continue
    return None


def apply_service(page: Page, service_name: str, pick: str = "") -> str | None:
    """选中作者服务类型，并在二级下拉里挑一个。

    service_name: 关联推广任务 / 关联变现任务 / 关联小程序
    pick: 二级列表里要匹配的关键词（空 = 选第一个）
    返回选中的二级项文本，失败返回 None。
    """
    try:
        kill_overlays(page)
        el = _select_by_placeholder(page, "选择服务类型")
        if el is None:
            log.info("未找到作者服务下拉，跳过 %s", service_name)
            return None
        if not _open_select(page, el):
            return None
        page.wait_for_timeout(1200)

        opts = _visible_options(page)
        if service_name not in opts and not _click_option(page, service_name):
            log.info("作者服务里没有 %r（当前选项：%s）", service_name, opts)
            page.keyboard.press("Escape")
            return None
        if service_name in opts:
            _click_option(page, service_name)
        page.wait_for_timeout(2500)

        # 小程序：选中后出现输入框，需粘贴链接（非下拉）
        if service_name == "关联小程序":
            inp = None
            for ph in ("粘贴小程序链接", "小程序链接", "输入小程序"):
                loc = page.get_by_placeholder(ph)
                if loc.count():
                    inp = loc.first
                    break
            if inp is None:
                for k in range(page.locator("input:visible").count()):
                    e = page.locator("input:visible").nth(k)
                    if (e.get_attribute("placeholder") or "").find("链接") >= 0:
                        inp = e
                        break
            if inp is not None and pick:
                inp.fill(pick)
                page.wait_for_timeout(2500)
                log.info("已填小程序链接：%s", pick[:40])
                return f"小程序:{pick[:20]}"
            log.info("%s 需要小程序链接（配置值给了 %r），跳过", service_name, str(pick)[:30])
            return None

        # 二级下拉（各服务 placeholder 不同）
        el2 = _find_sub_select(page, service_name)
        if el2 is None:
            sel1 = _read_selected(page, el)
            if sel1 and sel1 != "选择服务类型":
                log.info("已挂载作者服务：%s（无二级）", sel1)
                return sel1
            log.info("%s 无二级下拉且未生效，跳过", service_name)
            return None

        if not _open_select(page, el2):
            return None
        page.wait_for_timeout(1500)
        sub = _visible_options(page)
        if not sub:
            log.info("%s 二级列表为空（可能未开通或无可用项），跳过", service_name)
            page.keyboard.press("Escape")
            _clear_select(page, el)
            return None

        target = ""
        if pick:
            for t in sub:
                if pick in t:
                    target = t
                    break
        if not target:
            target = sub[0]
        if not _click_option(page, target):
            log.info("%s 二级项 %r 点不到，跳过", service_name, target[:30])
            page.keyboard.press("Escape")
            return None
        page.wait_for_timeout(1200)
        log.info("已挂载 %s：%s", service_name, target[:40])
        return target
    except Exception as e:
        log.warning("挂载作者服务 %s 失败（不影响发布）：%s", service_name, str(e)[:130])
        shot(page, "ks_service_fail")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return None


def list_service_options(page: Page) -> list[str]:
    """列出作者服务一级选项（排查用）。"""
    try:
        kill_overlays(page)
        el = _select_by_placeholder(page, "选择服务类型")
        if el is None:
            return []
        if not _open_select(page, el):
            return []
        page.wait_for_timeout(1500)
        opts = _visible_options(page)
        page.keyboard.press("Escape")
        return opts
    except Exception as e:
        log.debug("读取作者服务选项失败：%s", str(e)[:80])
        return []


# ---------- 热点 ----------

def apply_hot_topic(page: Page, keyword: str) -> bool:
    if not keyword:
        return False
    try:
        kill_overlays(page)
        el = _select_by_placeholder(page, "输入你想关联的热点")
        if el is None:
            log.info("未找到热点下拉，跳过关联热点")
            return False
        if not _open_select(page, el):
            return False

        if _click_option(page, keyword):
            page.wait_for_timeout(1000)
            log.info("已关联热点：%s", _read_selected(page, el) or keyword)
            return True

        typed = False
        for sel in (".ant-select-dropdown input:visible",
                    ".ant-select-selection-search-input:visible"):
            inp = page.locator(sel)
            if inp.count():
                try:
                    inp.first.fill(keyword)
                    typed = True
                    break
                except Exception:
                    continue
        if not typed:
            try:
                page.keyboard.type(keyword, delay=50)
            except Exception:
                pass
        page.wait_for_timeout(3000)

        if _click_option(page, keyword):
            page.wait_for_timeout(1000)
            log.info("已关联热点：%s", _read_selected(page, el) or keyword)
            return True

        for sel in (".ant-select-item-option-content:visible", ".ant-select-item-option:visible"):
            loc = page.locator(sel)
            if loc.count():
                t = (loc.first.inner_text() or "").strip()
                loc.first.click()
                page.wait_for_timeout(1200)
                log.info("已关联热点（首个候选）：%s", t[:30])
                return True

        log.info("热点 %r 无候选，跳过", keyword[:30])
        page.keyboard.press("Escape")
        return False
    except Exception as e:
        log.warning("关联热点失败（不影响发布）：%s", str(e)[:130])
        shot(page, "ks_hot_fail")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


# ---------- 地点 ----------

def apply_location(page: Page, city: str) -> bool:
    if not city:
        return False
    try:
        kill_overlays(page)
        el = _select_by_placeholder(page, "请选择所在地区")
        if el is None:
            log.info("未找到地点级联选择器，跳过添加地点")
            return False
        if not _open_select(page, el):
            return False
        page.wait_for_timeout(1500)

        picked: list[str] = []
        for _ in range(4):
            menus = page.locator(".ant-cascader-menu:visible")
            if not menus.count():
                break
            if city:
                loc = page.locator(".ant-cascader-menu-item:visible", has_text=city)
                if loc.count():
                    loc.first.click()
                    page.wait_for_timeout(1500)
                    picked.append(city)
                    break
            try:
                last = menus.nth(menus.count() - 1)
                items = last.locator(".ant-cascader-menu-item")
                if items.count():
                    t = (items.first.inner_text() or "").strip()
                    items.first.click()
                    page.wait_for_timeout(1200)
                    picked.append(t)
                    continue
            except Exception:
                pass
            break

        sel_txt = _read_selected(page, el)
        if sel_txt and sel_txt != "请选择所在地区":
            log.info("已添加地点：%s", sel_txt)
            return True
        for b in ("确定", "完成"):
            btn = page.get_by_role("button", name=b, exact=True)
            if not btn.count():
                btn = page.get_by_text(b, exact=True)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(1200)
                sel_txt = _read_selected(page, el)
                if sel_txt and sel_txt != "请选择所在地区":
                    log.info("已添加地点：%s", sel_txt)
                    return True
        if picked:
            log.info("地点已选（%s）", "/".join(picked))
            return True
        log.info("地点 %r 未选中，跳过", city)
        page.keyboard.press("Escape")
        return False
    except Exception as e:
        log.warning("添加地点失败（不影响发布）：%s", str(e)[:130])
        shot(page, "ks_location_fail")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


# ---------- 作者声明 ----------

def apply_declaration(page: Page, text: str) -> bool:
    """作者声明：ant-select[为作品添加补充说明] → 选中匹配项。"""
    if not text:
        return False
    try:
        kill_overlays(page)
        el = _select_by_placeholder(page, "为作品添加补充说明")
        if el is None:
            log.info("未找到作者声明下拉，跳过")
            return False
        if not _open_select(page, el):
            return False
        page.wait_for_timeout(1200)
        aliases = [text]
        if "AI" in text.upper():
            aliases += ["内容由AI生成", "内容为AI生成", "AI生成"]
        clicked = False
        for alias in dict.fromkeys(aliases):
            if _click_option(page, alias):
                page.wait_for_timeout(1000)
                log.info("已添加作者声明：%s", _read_selected(page, el) or alias)
                clicked = True
                break
        if clicked:
            return True
        opts = _visible_options(page)
        log.info("作者声明里没有 %r（可选：%s）", text, opts[:6])
        page.keyboard.press("Escape")
        return False
    except Exception as e:
        log.warning("作者声明失败（不影响发布）：%s", str(e)[:130])
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


# ---------- 批量入口 ----------

def apply_anchors(page: Page, anchors: dict | None, hot_topic: str = "") -> list[str]:
    """批量挂载。anchors 支持：热点 / 地点 / 推广任务 / 变现任务 / 小程序 / 声明。"""
    done: list[str] = []
    anchors = anchors or {}

    # 1. 作者服务（推广任务 / 变现任务 / 小程序）
    for service_name, key in SERVICE_MAP.items():
        pick = str(anchors.get(key) or "").strip()
        if not pick:
            continue
        try:
            got = apply_service(page, service_name, pick)
            if got:
                done.append(f"{key}:{got[:20]}")
        except Exception as e:
            log.warning("%s 挂载异常：%s", service_name, str(e)[:100])
        time.sleep(1)

    # 2. 热点
    hot = str(anchors.get("热点") or hot_topic or "").strip()
    if hot:
        try:
            if apply_hot_topic(page, hot):
                done.append("热点")
        except Exception as e:
            log.warning("热点挂载异常：%s", str(e)[:100])
        time.sleep(1)

    # 3. 地点
    loc = str(anchors.get("地点") or "").strip()
    if loc:
        try:
            if apply_location(page, loc):
                done.append("地点")
        except Exception as e:
            log.warning("地点挂载异常：%s", str(e)[:100])
        time.sleep(1)

    # 4. 作者声明
    decl = str(anchors.get("声明") or "").strip()
    if decl:
        try:
            if apply_declaration(page, decl):
                done.append(f"声明:{decl[:12]}")
        except Exception as e:
            log.warning("声明挂载异常：%s", str(e)[:100])

    return done

"""抖音审核违规巡检（家庭端 worker 调用）。

背景：抖音创作者中心 Web 端**没有**作品级审核违规通知入口
（通知页只有平台公告；实测 message/inbox、interaction、violation 等 URL 全部
重定向回首页/平台公告页）。但内容管理页有结构化的审核状态 tab：

    全部 | 已发布 | 审核中 | 未通过

因此检测源改为：
  主源：内容管理页「未通过」tab —— 抖音判定审核不通过的作品
  主源2：内容管理页「全部」tab 上标「流量减少」的作品（限流/限推荐，仍显示已发布）
  辅源：通知页 message/notice 扫「高度重复/限制传播」等违规文案

工作流：
  1. 打开内容管理页 → 切「未通过」tab → 枚举作品
  2. 切「全部」tab → 枚举状态含「流量减少」的作品
  3. 同时扫通知页，命中违规关键词的通知做补充匹配
  4. 用 (标题, 发布时间) 匹配 VPS /api/published 返回的清单 → 得到 task_id
  5. 对该作品执行删除（点「删除作品」→ 确认）
  6. POST /api/audit 回报 + Telegram 通知
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from .config import DATA_DIR
from .publish.base import goto_with_retry, launch_chromium, new_context, shot

log = logging.getLogger("audit_check")

DY_STATE = DATA_DIR / "douyin_state.json"
MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"
NOTICE_URL = "https://creator.douyin.com/creator-micro/message/notice"

# 违规关键词（通知页文案命中任一即判定为违规通知）
VIOLATION_KEYWORDS = [
    "高度重复", "限制传播", "疑似与其他账号", "已被限制", "审核不通过",
    "未通过审核", "不予推荐", "已被下架", "违反社区", "流量减少",
]

# 作品卡状态：命中即视为异常，自动删除
FLAGGED_STATUS = ("流量减少", "未通过", "仅自己可见", "限制传播")

DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})")
TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
ACTION_WORDS = ("编辑作品", "设置权限", "作品置顶", "删除作品", "取消置顶", "查看详情")
STATUS_WORDS = ("已发布", "审核中", "未通过", "仅自己可见", "流量减少")


def _norm_title(t: str) -> str:
    t = re.sub(r"\s+", "", t or "")
    t = re.sub(r"[，。！？、：；\"'（）《》【】\-—…·,.!?:;()\[\]]", "", t)
    return t.strip().lower()


def _parse_dt(s: str) -> datetime | None:
    s = (s or "").strip().replace("T", " ")
    m = DATE_RE.search(s)
    if m:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, h, mi)
        except ValueError:
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:19], fmt)
        except Exception:
            continue
    return None


def _load_manage(page, tries: int = 4) -> str:
    """打开内容管理页（带重试），返回正文。"""
    for i in range(tries):
        try:
            goto_with_retry(page, MANAGE_URL)
        except Exception as e:
            log.warning("内容管理页打开失败（第 %d 次）：%s", i + 1, str(e)[:90])
            page.wait_for_timeout(3000)
            continue
        page.wait_for_timeout(10_000)
        try:
            body = page.locator("body").inner_text(timeout=15_000)
        except Exception:
            body = ""
        if len(body) > 400:
            return body
        log.warning("内容管理页正文过短（%d 字），重试", len(body))
        page.wait_for_timeout(4000)
    return ""


def _switch_tab(page, name: str) -> str:
    """切到指定 tab，返回切换后的正文。"""
    try:
        t = page.get_by_role("tab", name=name)
        if not t.count():
            t = page.get_by_text(name, exact=True)
        if not t.count():
            log.warning("未找到 tab %r", name)
            return ""
        t.first.click()
        page.wait_for_timeout(7000)
        return page.locator("body").inner_text(timeout=15_000)
    except Exception as e:
        log.warning("切换 tab %r 失败：%s", name, str(e)[:100])
        return ""


def _parse_works(seg: str) -> list[dict]:
    """解析作品列表正文段，返回 [{title, published_at, status}]。

    结构：<时长>\n<标题>\n编辑作品\n设置权限\n作品置顶\n删除作品\n
          <YYYY年MM月DD日 HH:MM>\n<状态>\n播放\n...
    """
    works: list[dict] = []
    for m in re.finditer(
        r"删除作品\n(\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2})\n(\S+)",
        seg,
    ):
        dt_str, status = m.group(1), m.group(2)
        head = seg[:m.start()]
        lines = [ln.strip() for ln in head.split("\n") if ln.strip()]
        title = ""
        for ln in reversed(lines):
            if ln in ACTION_WORDS or ln in STATUS_WORDS:
                continue
            if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", ln):
                continue
            if "导出数据" in ln or "作品合集" in ln or ln in (
                "全部", "已发布", "审核中", "未通过",
            ):
                continue
            title = ln
            break
        if title:
            works.append({"title": title, "published_at": dt_str, "status": status})
    return works


def scrape_rejected(page) -> list[dict]:
    """主检测源：内容管理页「未通过」tab 的作品。"""
    body = _load_manage(page)
    if not body:
        shot(page, "audit_manage_fail")
        return []
    seg = _switch_tab(page, "未通过")
    if not seg or "没有更多作品" in seg:
        log.info("「未通过」tab 无作品")
        return []
    works = _parse_works(seg)
    log.info("「未通过」tab 解析到 %d 个作品", len(works))
    for w in works:
        w["violation"] = True
        w["reason"] = "审核未通过"
    return works


def scrape_flagged(page) -> list[dict]:
    """主检测源2：「全部」tab 上状态为流量减少/限流的作品。

    这类作品仍显示在「已发布」，不会出现在「未通过」，
    但卡片上会挂「流量减少」徽章（可点「查看详情」）。
    """
    seg = _switch_tab(page, "全部")
    if not seg:
        return []
    works = _parse_works(seg)
    flagged: list[dict] = []
    for w in works:
        st = w.get("status") or ""
        if any(k in st for k in FLAGGED_STATUS) and st != "已发布":
            w["violation"] = True
            w["reason"] = "状态：" + st
            flagged.append(w)
    log.info("「全部」tab 解析 %d 个作品，异常状态 %d 个",
             len(works), len(flagged))
    for w in flagged:
        log.warning("  异常作品：%s  %s  [%s]",
                    w.get("published_at", ""), w.get("title", "")[:40],
                    w.get("status", ""))
    return flagged


def scrape_notice_violations(page) -> list[dict]:
    """辅检测源：通知页违规文案。"""
    try:
        goto_with_retry(page, NOTICE_URL)
        page.wait_for_timeout(8000)
        body = page.locator("body").inner_text(timeout=15_000)
    except Exception as e:
        log.warning("通知页打开失败：%s", str(e)[:100])
        return []
    if len(body) < 100:
        return []
    out: list[dict] = []
    parts = TS_RE.split(body)
    for i in range(1, len(parts), 2):
        ts, content = parts[i], (parts[i + 1] if i + 1 < len(parts) else "")
        text = content[:800]
        if any(k in text for k in VIOLATION_KEYWORDS):
            title = ""
            m = re.search(r"作品标题[：:]\s*(.+)", text)
            if m:
                title = m.group(1).strip()
            pub = ""
            m2 = re.search(
                r"发布时间[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)",
                text,
            )
            if m2:
                pub = m2.group(1)
            out.append({"title": title, "published_at": pub, "notice_at": ts,
                        "text": text, "violation": True, "reason": "通知违规文案"})
    log.info("通知页违规通知：%d 条", len(out))
    return out


def match_published(cand: dict, published: list[dict]) -> dict | None:
    """把违规作品/通知匹配到 VPS 的已发布清单。"""
    n_title = _norm_title(cand.get("title", ""))
    n_dt = _parse_dt(cand.get("published_at", ""))
    if n_title and n_dt:
        for it in published:
            t = _norm_title(it.get("title", ""))
            dt = _parse_dt(it.get("published_at", ""))
            if t and (t == n_title or n_title in t or t in n_title) and dt \
                    and abs((dt - n_dt).total_seconds()) <= 7200:
                return it
    if n_dt:
        best, gap0 = None, 1e9
        for it in published:
            dt = _parse_dt(it.get("published_at", ""))
            if not dt:
                continue
            g = abs((dt - n_dt).total_seconds())
            if g <= 1800 and g < gap0:
                best, gap0 = it, g
        if best:
            return best
    if n_title:
        for it in published:
            t = _norm_title(it.get("title", ""))
            if t and (t == n_title or n_title in t or t in n_title):
                return it
    return None


def delete_work(page, title: str) -> bool:
    """在内容管理页定位作品并删除（点「删除作品」→ 确认）。"""
    body = _load_manage(page)
    if not body:
        return False

    for tab in ("未通过", "全部"):
        _switch_tab(page, tab)
        loc = page.get_by_text(title, exact=False)
        if not loc.count() and len(title) > 10:
            loc = page.get_by_text(title[:10], exact=False)
        if loc.count():
            break
    else:
        log.warning("内容管理页未找到作品：%s", title[:40])
        shot(page, "audit_work_not_found")
        return False

    card = loc.first
    try:
        anc = card.locator("xpath=ancestor::*[.//*[contains(text(),'删除作品')]][1]")
        if anc.count():
            card = anc
    except Exception:
        pass

    del_btn = card.get_by_text("删除作品", exact=True)
    if not del_btn.count():
        del_btn = page.get_by_text("删除作品", exact=True)
    if not del_btn.count():
        log.warning("未找到「删除作品」按钮")
        shot(page, "audit_del_btn_missing")
        return False

    try:
        del_btn.first.click()
        page.wait_for_timeout(2500)
    except Exception as e:
        log.warning("点击删除作品失败：%s", str(e)[:100])
        shot(page, "audit_del_click_fail")
        return False

    for label in ("确认删除", "确定删除", "确认", "确定", "删除"):
        try:
            c = page.get_by_role("button", name=label, exact=True)
            if not c.count():
                c = page.get_by_text(label, exact=True)
            if c.count() and c.first.is_visible():
                c.first.click()
                page.wait_for_timeout(4000)
                log.info("已确认删除：%s", title[:40])
                return True
        except Exception:
            continue

    shot(page, "audit_confirm_missing")
    log.warning("删除确认弹窗未找到")
    return False


def run_audit(douyin_published: list[dict], headless: bool = True,
              dry_run: bool = False) -> list[dict]:
    """跑一轮审核巡检。返回 [{'matched', 'cand', 'deleted', 'reason'}]。"""
    if not DY_STATE.exists():
        log.warning("抖音登录态不存在，跳过审核巡检")
        return []

    results: list[dict] = []
    with sync_playwright() as p:
        browser = launch_chromium(p, headless=headless)
        context = new_context(browser, DY_STATE)
        page = context.new_page()
        try:
            cands = (scrape_rejected(page)
                     + scrape_flagged(page)
                     + scrape_notice_violations(page))
            if not cands:
                log.info("审核巡检：未发现违规作品/通知")
                return []
            log.info("审核巡检：发现 %d 个待处理项", len(cands))

            seen: set[int] = set()
            for c in cands:
                matched = match_published(c, douyin_published)
                if not matched:
                    log.warning("未匹配到本地记录：标题=%r 发布=%r",
                                c.get("title", "")[:40], c.get("published_at", ""))
                    results.append({"matched": None, "cand": c, "deleted": False})
                    continue
                tid = matched["task_id"]
                if tid in seen:
                    continue
                seen.add(tid)
                log.warning("匹配到违规作品 task=%s %s（%s）",
                            tid, matched["shortcode"], c.get("reason", ""))
                if dry_run:
                    results.append({"matched": matched, "cand": c, "deleted": False,
                                    "dry_run": True})
                    continue
                ok = delete_work(page, c.get("title", "") or matched.get("title", ""))
                shot(page, f"audit_delete_{tid}")
                results.append({"matched": matched, "cand": c, "deleted": ok})
        except Exception as e:
            log.exception("审核巡检异常")
            shot(page, "audit_error")
            results.append({"error": str(e)[:300], "deleted": False})
        finally:
            context.close()
            browser.close()
    return results

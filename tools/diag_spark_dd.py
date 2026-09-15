#!/usr/bin/env python
"""诊断 3：上传后点开「选择服务类型」，抓取下拉真实选项（含异步加载等待）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

from bot.config import KS_STATE_PATH, MEDIA_DIR
from bot.publish.base import launch_chromium, new_context, goto_with_retry, settle
from bot.publish.kuaishou import (
    PUBLISH_URL, _remove_joyride, _upload_with_retry, _wait_form_ready,
    dismiss_dialogs, shot,
)


def dump_dropdown(page, tag: str):
    """抓所有下拉（含隐藏）的选项，不假设 ant-select 类名。"""
    data = page.evaluate("""() => {
        const norm = s => (s || '').replace(/\\s+/g,' ').trim();
        const out = [];
        // 全部可能的浮层容器
        const sel = '[class*="dropdown"], [class*="popup"], [class*="overlay"], [role="listbox"], [class*="select"]';
        for (const e of document.querySelectorAll(sel)) {
            const cls = (e.className||'').toString();
            const items = [...e.querySelectorAll('[class*="item"], li, [role="option"]')]
                .map(x => norm(x.textContent)).filter(t => t && t.length < 60);
            if (items.length) out.push({cls: cls.slice(0,110), items: [...new Set(items)].slice(0,25)});
        }
        return out;
    }""")
    print(f"\n  [{tag}] 候选浮层 {len(data)} 个：")
    for d in data:
        print(f"    cls={d['cls']}")
        for it in d["items"]:
            print(f"        - {it}")


def main() -> int:
    cands = sorted(MEDIA_DIR.glob("remote/*/*_final.mp4"), key=lambda x: -x.stat().st_mtime)
    video = Path(sys.argv[1]) if len(sys.argv) > 1 else (cands[0] if cands else None)
    if not video:
        print("[x] 无可用视频")
        return 1
    print(f"使用视频：{video}")

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=True)
        ctx = new_context(browser, Path(KS_STATE_PATH))
        page = ctx.new_page()
        try:
            goto_with_retry(page, PUBLISH_URL)
            settle(page)
            dismiss_dialogs(page)
            _remove_joyride(page)
            _upload_with_retry(page, video)
            _wait_form_ready(page)
            print("表单就绪")
            shot(page, "sparkdd_00")

            # 点「选择服务类型」占位符本身
            print("\n=== 点击「选择服务类型」占位符 ===")
            try:
                el = page.locator(".ant-select-selection-placeholder").filter(
                    has_text="选择服务类型").first
                el.click(force=True, timeout=5000)
                print("  点击成功")
            except Exception as e:
                print(f"  点击失败: {e}")

            for wait in (1500, 3000, 5000):
                page.wait_for_timeout(wait)
                dump_dropdown(page, f"点击后 +{wait}ms")
                dd = page.evaluate("""() => [...document.querySelectorAll(
                    '[class*="ant-select-dropdown"]'
                )].map(e => ({
                    hidden: (e.className||'').toString().includes('hidden'),
                    items: [...e.querySelectorAll('[class*="ant-select-item-option"]')]
                             .map(x => (x.textContent||'').trim())
                }))""")
                print(f"    ant-select-dropdown: {dd}")
                if any(d.get("items") for d in dd):
                    break
            shot(page, "sparkdd_01_after_click")

            print("\n=== body 里搜「作者服务」上下文 ===")
            ctx_text = page.evaluate("""() => {
                const body = (document.body.innerText||'').replace(/\\s+/g,' ');
                const i = body.indexOf('作者服务');
                return i < 0 ? '(未找到)' : body.slice(Math.max(0,i-100), i+300);
            }""")
            print(f"  {ctx_text}")

            return 0
        finally:
            try:
                shot(page, "sparkdd_99")
            except Exception:
                pass
            ctx.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())

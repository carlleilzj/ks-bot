#!/usr/bin/env python
"""诊断 4：展开「选择服务类型」下拉，抓取真实可见选项（虚拟列表）。"""
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

JS_OPEN = """() => {
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
    for (const e of document.querySelectorAll('.ant-select')) {
        const ph = norm(e.querySelector('.ant-select-selection-placeholder')?.textContent);
        if (ph.includes('服务类型')) {
            const s = e.querySelector('.ant-select-selector') || e;
            for (const t of ['mousedown', 'mouseup', 'click']) {
                s.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
            }
            return true;
        }
    }
    return false;
}"""

JS_DUMP = """(sel) => [...document.querySelectorAll(sel)].map(e => ({
    t: (e.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 70),
    vis: !!(e.offsetParent || e.getClientRects().length)
}))"""


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

            print(f"展开服务类型下拉: {page.evaluate(JS_OPEN)}")
            page.wait_for_timeout(2500)
            shot(page, "spark4_01_opened")

            for sel in [
                ".ant-select-item-option-content",
                ".ant-select-item",
                ".rc-virtual-list-holder-inner > *",
                "[role='option']",
                ".ant-select-dropdown *",
            ]:
                vals = page.evaluate(JS_DUMP, sel)
                visible = [v for v in vals if v["vis"] and v["t"]]
                print(f"\n=== {sel}: {len(vals)} 个（可见 {len(visible)}）===")
                for v in visible[:15]:
                    print(f"    {v['t']!r}")

            print("\n=== 下拉完整 innerHTML（截断 2000 字）===")
            html = page.evaluate("""() => {
                const d = [...document.querySelectorAll('[class*=ant-select-dropdown]')]
                    .find(e => !(e.className||'').toString().includes('hidden'));
                return d ? d.innerHTML : '(无可见下拉)';
            }""")
            print(html[:2000])
            return 0
        finally:
            try:
                shot(page, "spark4_99")
            except Exception:
                pass
            ctx.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())

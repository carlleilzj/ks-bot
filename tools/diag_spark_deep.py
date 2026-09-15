#!/usr/bin/env python
"""星火挂载深度诊断：上传视频后，把「选择服务类型」区域及其下拉的真实 DOM 结构 dump 出来。

与 diag_spark.py 的区别：本脚本不强依赖 ant-select 类名假设，而是
直接把相关区域的 innerHTML / 文本 / 可选元素全量打印，用于定位页面改版。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

from bot.config import KS_STATE_PATH, MEDIA_DIR
from bot.publish.base import launch_chromium, new_context, goto_with_retry, settle
from bot.publish.kuaishou import (
    PUBLISH_URL,
    _remove_joyride,
    _upload_with_retry,
    _wait_form_ready,
    dismiss_dialogs,
    shot,
)


def main() -> int:
    cands = sorted(MEDIA_DIR.glob("remote/*/*_final.mp4"), key=lambda x: -x.stat().st_mtime)
    if len(sys.argv) > 1:
        video = Path(sys.argv[1])
    elif cands:
        video = cands[0]
    else:
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
            print("\n=== 表单已就绪，dump 全页可交互元素 ===")
            shot(page, "sparkdeep_00_form")

            info = page.evaluate("""() => {
                const norm = s => (s || '').replace(/\\s+/g,' ').trim();
                const out = {};
                // 1. 所有 ant-select 实例（不管开没开）
                out.selects = [...document.querySelectorAll('.ant-select')].map(e => ({
                    cls: (e.className||'').toString().slice(0,120),
                    text: norm(e.textContent).slice(0,80),
                    ph: norm(e.querySelector('.ant-select-selection-placeholder')?.textContent),
                    sel: norm(e.querySelector('.ant-select-selection-item')?.textContent),
                }));
                // 2. 所有下拉容器（含隐藏的）
                out.dropdowns = [...document.querySelectorAll('[class*="ant-select-dropdown"]')].map(e => ({
                    cls: (e.className||'').toString().slice(0,120),
                    items: [...e.querySelectorAll('[class*="ant-select-item-option"]')]
                             .map(x => norm(x.textContent)).slice(0,20),
                }));
                // 3. 表单区里所有含"选择"字样的元素
                out.chooseTexts = [...document.querySelectorAll('*')]
                    .filter(e => e.children.length === 0 && /选择|服务类型|变现|星火/.test(norm(e.textContent)))
                    .map(e => ({tag: e.tagName, cls: (e.className||'').toString().slice(0,100),
                                text: norm(e.textContent).slice(0,80)}))
                    .slice(0, 40);
                // 4. 全页 body 文本里搜关键词
                const body = norm(document.body.innerText);
                out.kw = {};
                for (const k of ['服务类型','关联变现','变现','星火','作者服务','作品描述','发布']) {
                    out.kw[k] = body.split(k).length - 1;
                }
                return out;
            }""")

            print(f"\n--- ant-select 实例 ({len(info['selects'])} 个) ---")
            for s in info["selects"]:
                print(f"  ph={s['ph']!r} sel={s['sel']!r} text={s['text']!r}")

            print(f"\n--- 下拉容器 ({len(info['dropdowns'])} 个) ---")
            for d in info["dropdowns"]:
                print(f"  cls={d['cls'][:70]} items={d['items']}")

            print(f"\n--- 含关键词的叶子元素 ({len(info['chooseTexts'])} 个) ---")
            for c in info["chooseTexts"]:
                print(f"  <{c['tag']} class={c['cls'][:60]!r}> {c['text']!r}")

            print(f"\n--- body 关键词计数 ---")
            for k, v in info["kw"].items():
                print(f"  {k}: {v}")

            print("\n=== 尝试点击「选择服务类型」（多策略） ===")
            for strat, md in [
                ("text-exact", "exact"),
                ("text-loose", None),
            ]:
                try:
                    if md == "exact":
                        loc = page.get_by_text("选择服务类型", exact=True)
                    else:
                        loc = page.get_by_text("选择服务类型", exact=False)
                    n = loc.count()
                    print(f"  策略 {strat}: 命中 {n} 个")
                    if n:
                        loc.last.click(force=True, timeout=3000)
                        page.wait_for_timeout(1500)
                        shot(page, f"sparkdeep_01_after_click_{strat}")
                        dd = page.evaluate("""() => [...document.querySelectorAll(
                            '[class*="ant-select-dropdown"]:not([class*="hidden"])')].map(e => ({
                              cls: (e.className||'').toString().slice(0,100),
                              items: [...e.querySelectorAll('[class*="ant-select-item-option"]')]
                                       .map(x => (x.textContent||'').trim())
                            }))""")
                        print(f"    点击后可见下拉: {dd}")
                        break
                except Exception as e:
                    print(f"  策略 {strat} 失败: {e}")

            return 0
        finally:
            try:
                shot(page, "sparkdeep_99_final")
            except Exception:
                pass
            ctx.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""星火挂载诊断：打开快手发布页，把星火相关表单的真实 DOM 状态 dump 出来。

用途：2026-09-13 起出现「交互成功但作品不带流量助推」的静默失效，
本脚本用于在不发布的前提下，抓取发布页上「关联变现任务」下拉的真实内容，
判断任务是否还在可选列表里、是否被平台过滤。

用法（在阿里云发布端）：
    cd /opt/ks-bot && .venv/bin/python tools/diag_spark.py

输出：
    - 终端打印服务类型下拉项 / 变现任务下拉项 / 表单选中项
    - 截图存到 logs/spark_diag_*.png
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

from bot.config import KS_STATE_PATH
from bot.publish.base import launch_chromium, new_context, goto_with_retry, settle
from bot.publish.kuaishou import (
    PUBLISH_URL,
    SPARK_TYPE_LABEL,
    _dropdown_option_texts,
    _click_placeholder,
    _spark_form_state,
    _remove_joyride,
    dismiss_dialogs,
    shot,
)

LOG_DIR = Path("/opt/ks-bot/logs")


def main() -> int:
    if not Path(KS_STATE_PATH).exists():
        print(f"[x] 找不到登录态：{KS_STATE_PATH}")
        return 1

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=True)
        ctx = new_context(browser, Path(KS_STATE_PATH))
        page = ctx.new_page()
        try:
            goto_with_retry(page, PUBLISH_URL)
            settle(page)
            dismiss_dialogs(page)
            _remove_joyride(page)

            print("=== 0. 发布页初始状态 ===")
            print(f"URL  : {page.url}")
            print(f"表单 : {_spark_form_state(page)}")
            shot(page, "spark_diag_00_initial")

            print("\n=== 1. 点开「选择服务类型」 ===")
            if not _click_placeholder(page, "选择服务类型"):
                print("[!] 未找到「选择服务类型」入口 —— 表单本身没渲染出来")
                print("    （通常是该账号/该页面版本没有星火模块，或页面改版）")
                shot(page, "spark_diag_01_no_entry")
                return 2
            page.wait_for_timeout(1200)
            opts = _dropdown_option_texts(page)
            print(f"服务类型下拉项 ({len(opts)} 个)：")
            for o in opts:
                mark = "  <== 星火" if SPARK_TYPE_LABEL in o else ""
                print(f"    - {o}{mark}")
            if not any(SPARK_TYPE_LABEL in o for o in opts):
                body = page.inner_text("body") or ""
                print(f"[!] 下拉里没有「{SPARK_TYPE_LABEL}」")
                print(f"    页面文本里是否出现该词: {SPARK_TYPE_LABEL in body}")
                shot(page, "spark_diag_01_no_spark_type")
                return 3

            print("\n=== 2. 选中「关联变现任务」，看任务下拉 ===")
            clicked = page.evaluate("""(want) => {
                const nodes = [...document.querySelectorAll(
                    '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option'
                )];
                const el = nodes.find(e => ((e.textContent||'').trim()) === want);
                if (!el) return false;
                el.scrollIntoView({block:'nearest'}); el.click(); return true;
            }""", SPARK_TYPE_LABEL)
            print(f"选中服务类型: {clicked}")
            page.wait_for_timeout(1500)

            second_ph = "关联变现任务获得更多收入"
            opened = False
            for _ in range(20):
                if _click_placeholder(page, second_ph):
                    opened = True
                    break
                page.wait_for_timeout(400)
            print(f"任务下拉可点开: {opened}")
            if not opened:
                print("[!] 任务下拉打不开 —— App 里可能没有收藏任务")
                shot(page, "spark_diag_02_no_task_dropdown")
                return 4

            page.wait_for_timeout(1000)
            titles = _dropdown_option_texts(page)
            print(f"\n变现任务下拉项 ({len(titles)} 个)：")
            for t in titles:
                print(f"    - {t}")
            shot(page, "spark_diag_02_task_list")

            print("\n=== 3. 结论 ===")
            if not titles:
                print("[!] 任务列表为空 —— 需要到快手 App 星火计划收藏任务")
                return 5
            has_target = any("狐缘山间" in t for t in titles)
            print(f"目标任务《狐缘山间》在列表中: {has_target}")
            if not has_target:
                print("    → 任务可能已下架/改名/额度耗尽，需要手动确认")
            print("    → 若列表正常但发布后仍无「流量助推」，问题在提交环节"
                  "（挂载状态未随表单序列化），需抓网络请求进一步定位")
            return 0
        finally:
            try:
                shot(page, "spark_diag_99_final")
            except Exception:
                pass
            ctx.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())

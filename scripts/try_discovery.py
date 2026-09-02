"""发现层实测脚本：跑一轮真实采集，打印候选质量报告。

不启动完整 bot（不起发布浏览器、不起 TG 监听线程），只：
1. 加载真实 config.yaml（discovery.enabled=true）
2. 用临时 DB 隔离（不动 data/bot.db）
3. 跑一轮 DiscoveryScheduler._cycle()
4. 打印：各适配器候选数、过滤明细、最终入库清单

用法：.venv/bin/python3 scripts/try_discovery.py [--keep-db]
默认临时 DB 用完即删；--keep-db 保留到 /tmp 供检查。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_settings
from bot.db import Database, State
from bot.source.scheduler import DiscoveryScheduler


def main() -> None:
    keep_db = "--keep-db" in sys.argv
    s = load_settings()
    disc = s.discovery

    print("=" * 62)
    print(" 发现层实测（一轮）")
    print("=" * 62)
    print(f"enabled: {disc.enabled}")
    print(f"filters: duration {disc.filters_min_duration}-{disc.filters_max_duration}s, "
          f"min_score {disc.filters_min_score}")
    print(f"reject_real_person: {disc.filters.get('reject_real_person', False)}")
    print(f"max_pending_review: {disc.max_pending_review}")

    # 临时 DB 隔离
    tmp = tempfile.mkdtemp(prefix="ksbot-discovery-test-")
    db_path = Path(tmp) / "test.db"
    db = Database(db_path)
    print(f"临时 DB: {db_path}")

    # 构造 scheduler（TG 发送会被无 token 拦截，安全空操作）
    sched = DiscoveryScheduler(s, db)
    print(f"adapters: {[a.name for a in sched.adapters]}")
    if not sched.adapters:
        print("❌ 没有可用适配器，检查 config.yaml discovery.sources")
        return

    # ============ 阶段 1：各适配器原始候选 ============
    print("\n" + "-" * 62)
    print("阶段 1：各适配器原始候选")
    print("-" * 62)
    all_cands = []
    for ad in sched.adapters:
        try:
            cands = ad.discover(limit=sched.limit_per_source)
            print(f"\n[{ad.name}] 返回 {len(cands)} 条:")
            for i, c in enumerate(cands, 1):
                dur = f"{int(c.duration)}s" if c.duration else "n/a"
                score = f"{int(c.score)}" if c.score else "n/a"
                print(f"  {i:2d}. [{dur:>5}] view={score:>9} {c.title[:52]}")
                print(f"      @{c.uploader[:30]}  {c.url[:70]}")
            all_cands.extend(cands)
        except Exception as e:
            print(f"\n[{ad.name}] ❌ 失败: {e}")

    print(f"\n原始候选合计: {len(all_cands)}")

    # ============ 阶段 2：过滤链明细 ============
    print("\n" + "-" * 62)
    print("阶段 2：过滤链判定（逐条）")
    print("-" * 62)
    passed = []
    seen = set()
    for c in sorted(all_cands, key=lambda x: -x.score):
        if c.video_id in seen:
            continue
        seen.add(c.video_id)
        ok, reason = sched.chain.keep(c)
        mark = "✅" if ok else "❌"
        print(f"  {mark} {c.title[:44]:<44} → {reason}")
        if ok:
            passed.append(c)
    print(f"\n过滤通过: {len(passed)} / {len(all_cands)}")

    # ============ 阶段 3：跑 _cycle 入库（含 extract_meta + 封面 + 真人检测） ============
    print("\n" + "-" * 62)
    print("阶段 3：完整 _cycle（元数据解析 + 入库 + 审核队列）")
    print("-" * 62)
    sched._cycle()

    tasks = db.pending_review()
    print(f"\n进入待审核队列: {len(tasks)} 条")
    print(f"待审核计数: {db.pending_review_count()}")
    for t in tasks:
        dur = "n/a"  # insert_video 不存 duration
        cover = "有" if t.get("cover_path") else "无"
        print(f"  tid={t['id']:>3} [{t['source_platform']}] 封面:{cover} "
              f"{(t.get('title') or '')[:50]}")
        print(f"        {t.get('source_url','')[:75]}")

    # 被拒/跳过的
    skipped = [r for r in db.recent(100) if r["state"] == State.SKIPPED]
    if skipped:
        print(f"\n被跳过（含真人检测拒绝）: {len(skipped)} 条")
        for t in skipped:
            print(f"  [{t['shortcode'][:30]}] {(t.get('error') or '')[:50]}")

    print("\n" + "=" * 62)
    if keep_db:
        print(f"DB 已保留: {db_path}")
    else:
        print(f"临时 DB（{db_path}）随临时目录清理")
    print("下一步：质量满意的话，把 config.yaml 的 discovery.enabled 保持 true,")
    print("重启 bot（launchctl 或 python -m bot.main）即可长期运行。")


if __name__ == "__main__":
    main()

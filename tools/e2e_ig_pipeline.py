"""端到端模拟：模拟 TG 投链 → 出海清洗 → 英文文案，验证全流程不发布。

这是「点开 IG 账号 enabled 之后」最该做的验证：确认一条国内素材真的能被
加工成可发 IG 的成品（画面裁好、音轨处理对、英文文案地道）。

不联网下载（用本地已有视频代替素材），不真实发布。
用法：
    python tools/e2e_ig_pipeline.py <本地视频路径> <账号key>
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import FINAL_DIR, WORK_DIR, load_settings   # noqa: E402
from bot.media import export as ex_exporter                 # noqa: E402
from bot.media import ffmpeg                                # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print("用法：python tools/e2e_ig_pipeline.py <本地视频> <账号key>")
        print("账号key：ig_pets / ig_satisfying / ig_anim / ig_home")
        return 2

    src = Path(sys.argv[1])
    account = sys.argv[2]
    if not src.exists():
        print(f"✗ 视频不存在：{src}")
        return 1

    s = load_settings()
    tag = "e2e_ig"
    work_src = WORK_DIR / f"{tag}_src.mp4"
    work_tc = WORK_DIR / f"{tag}_tc.mp4"
    work_export = WORK_DIR / f"{tag}_export.mp4"

    print("=" * 60)
    print(f" 端到端模拟：{src.name} → {account}")
    print("=" * 60)

    shutil.copyfile(src, work_src)
    info0 = ffmpeg.video_info(work_src)
    print(f"\n[1/4] 素材：{info0['width']}x{info0['height']}, "
          f"{info0.get('duration', 0):.1f}s, audio={info0.get('has_audio')}")

    ffmpeg.ensure_compatible(work_src, work_tc)
    print(f"[2/4] 转码完成：{work_tc.stat().st_size // 1024} KB")

    prof = ex_exporter.ExportProfile.for_account(account)
    params = ex_exporter.build_export_video(work_tc, work_export, prof, seed=42)
    info1 = ffmpeg.video_info(work_export)
    print(f"[3/4] 出海清洗（{account}）：{params}")
    print(f"      输出：{info1['width']}x{info1['height']}, "
          f"audio={info1.get('has_audio')}")

    problems = ex_exporter.verify_export(work_tc, work_export)
    if problems:
        for p in problems:
            print(f"      ⚠️  {p}")
    else:
        print("      ✓ 成品校验通过")

    # 文案：走 ig_* 的英文 prompt
    print(f"\n[4/4] 生成英文文案（{account}）...")
    try:
        from bot.ai.copywriter import generate_copy
        copy = generate_copy(
            transcript="", caption="",
            categories=[], s=s, platform=account,
            content={"subject": "a cute animal doing something funny",
                     "action": "reacts in a surprising way",
                     "setting": "indoors",
                     "beats": "a sudden twist at the end",
                     "summary": "uploaded test material for pipeline smoke"})

        print(f"      title      : {copy['title']}")
        print(f"      description: {copy['description']}")
        print(f"      tags       : {copy['tags']}")

        # 出海文案的关键检查
        title = copy["title"]
        checks = [
            ("caption 首行是英文钩子", not any('\u4e00' <= c <= '\u9fff' for c in title[:40])),
            ("不含中文", not any('\u4e00' <= c <= '\u9fff' for c in title)),
            ("标签数量达标", len(copy["tags"]) >= 5),
            ("标签全小写无空格", all(t.islower() and " " not in t for t in copy["tags"])),
        ]
        ok = True
        for name, cond in checks:
            print(f"      {'✓' if cond else '✗'} {name}")
            ok &= cond
    except Exception as e:
        print(f"      ✗ 文案生成失败：{e}")
        ok = False

    final = FINAL_DIR / f"{tag}_final.mp4"
    shutil.copyfile(work_export, final)
    print(f"\n成品已放置：{final.name}（{final.stat().st_size // 1024} KB）")

    print("\n" + "=" * 60)
    if ok and not problems:
        print("✅ 端到端流程通过（未发布）")
        return 0
    print("⚠️  流程有告警，见上面 ✗/⚠️ 项")
    return 1


if __name__ == "__main__":
    sys.exit(main())

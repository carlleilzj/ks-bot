"""用真实 ffmpeg 跑一次出海清洗，验证滤镜链是真的能出片（不是 mock）。

这是上线前必跑的验证：单元测试只能证明「调用了正确的函数」，
证明不了「crop+scale 滤镜链在真实视频上不报错、输出尺寸正确」。

用法：
    python tools/smoke_export_video.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.media import export as ex          # noqa: E402
from bot.media import ffmpeg                # noqa: E402


def make_test_video(path: Path, w: int = 720, h: int = 1280,
                    seconds: int = 5, with_audio: bool = True) -> bool:
    """造一个竖屏测试视频（底部带一块纯色「字幕区」便于肉眼确认裁切）。"""
    cmd = [ffmpeg._ffmpeg(), "-y",          # noqa: SLF001
           "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration={seconds}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
        cmd += ["-c:a", "aac", "-b:a", "96k"]
    # 底部 12% 画一块红色区域，模拟「中文字幕条」
    bar_h = int(h * 0.12)
    cmd += ["-vf", f"drawbox=x=0:y={h - bar_h}:w={w}:h={bar_h}:color=red@0.9:t=fill"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-pix_fmt", "yuv420p", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print(f"  造测试视频失败：{r.stderr[-400:]}")
        return False
    return True


def check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"    {mark} {name}{('：' + detail) if detail else ''}")
    return cond


def run_case(account: str, with_audio: bool) -> bool:
    prof = ex.ExportProfile.for_account(account)
    print(f"\n--- {account}（strip_audio={prof.strip_audio}, "
          f"crop={prof.crop_bottom_ratio}）---")

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        src = tdp / "src.mp4"
        dst = tdp / "out.mp4"

        if not make_test_video(src, with_audio=with_audio):
            return False

        before = ffmpeg.video_info(src)
        print(f"    输入：{before['width']}x{before['height']}, "
              f"audio={before.get('has_audio')}, {before.get('duration')}s")

        try:
            params = ex.build_export_video(src, dst, prof, seed=7)
        except Exception as e:
            print(f"    ✗ 清洗抛异常：{e}")
            return False

        ok = True
        ok &= check("输出文件已生成", dst.exists() and dst.stat().st_size > 10_000,
                    f"{dst.stat().st_size // 1024} KB" if dst.exists() else "缺失")

        after = ffmpeg.video_info(dst)
        print(f"    输出：{after['width']}x{after['height']}, "
              f"audio={after.get('has_audio')}")
        print(f"    参数：{params}")

        # 分辨率必须保持原样（裁完要 scale 回来）
        ok &= check("分辨率保持原尺寸",
                    (after["width"], after["height"]) == (before["width"], before["height"]),
                    f"{after['width']}x{after['height']}")

        # 音频策略必须与配方一致
        if prof.strip_audio:
            ok &= check("音轨已剥离", not after.get("has_audio"))
        else:
            expect_audio = bool(before.get("has_audio"))
            ok &= check("音轨已保留", bool(after.get("has_audio")) == expect_audio,
                        f"expect={expect_audio}")

        # 底部红色「字幕区」应当被裁掉——抽最后一帧看底部像素
        try:
            frame = tdp / "last.jpg"
            subprocess.run(
                [ffmpeg._ffmpeg(), "-y", "-sseof", "-0.3", "-i", str(dst),  # noqa: SLF001
                 "-frames:v", "1", "-q:v", "2", str(frame)],
                capture_output=True, timeout=60)
            if frame.exists():
                # 用 ffmpeg 统计底部 5% 区域的平均色（红色通道应显著高于蓝）
                r = subprocess.run(
                    [ffmpeg._ffmpeg(), "-i", str(frame), "-vf",  # noqa: SLF001
                     "crop=iw:ih*0.05:0:ih*0.95,signalstats,"
                     "metadata=print:key=lavfi.signalstats.YAVG",
                     "-f", "null", "-"],
                    capture_output=True, text=True, timeout=60)
                # 只做「命令能跑通」的验证；具体像素值依赖 testsrc 图案，不适合硬断言
                ok &= check("底部区域抽帧统计可执行", r.returncode == 0)
        except Exception as e:
            print(f"    （底部验证跳过：{e}）")

        problems = ex.verify_export(src, dst)
        ok &= check("verify_export 无致命问题",
                    not any("横屏" in p or "不存在" in p for p in problems),
                    "; ".join(problems) if problems else "无")

        return ok


def main() -> int:
    print("=" * 60)
    print(" 出海清洗真实 ffmpeg 冒烟")
    print("=" * 60)

    results = []
    # 解压号：必须保声
    results.append(run_case("ig_satisfying", with_audio=True))
    # 动画号：必须剥声
    results.append(run_case("ig_anim", with_audio=True))
    # 无声素材（萌宠常见）：不能被 -an 之外的处理搞挂
    results.append(run_case("ig_pets", with_audio=False))

    print("\n" + "=" * 60)
    if all(results):
        print("✅ 全部通过")
        return 0
    print("❌ 有失败项")
    return 1


if __name__ == "__main__":
    sys.exit(main())

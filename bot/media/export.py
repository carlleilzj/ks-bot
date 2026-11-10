"""出海素材预处理：把国内短视频清洗成可直接发 Instagram / TikTok 的版本。

为什么需要单独一层（不能复用国内链路）：
1. **中文字幕是硬伤**：国内平台素材 90%+ 在画面底部烧了中文硬字幕，
   老外看不懂、观感廉价，完播率会被腰斩。国内链路恰好相反——它是
   「生成中文字幕再烧进去」，所以出海必须**跳过烧字幕**并**裁掉已有字幕区**；
2. **水印位置不同**：抖音/小红书水印常在右下 + 左上双点位，且会随视频
   上下浮动，固定坐标 delogo 常常擦不干净；
3. **音频策略相反**：
   - 解压类（Oddly Satisfying）**必须保留原声**（ASMR 是命脉）；
   - 搞笑/动画类原声无意义（多为中文配音或罐头音效），
     **剥离原音轨**更干净（也顺带洗掉音频指纹）；
4. **画面比例**：IG Reels 强制 9:16 竖屏，国内横屏素材必须补边，
   否则两侧被裁掉丢失主体。

本模块只做「画面/音频层」的处理，文案由 ai/copywriter 的 ig_* profile 负责。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg

log = logging.getLogger(__name__)


@dataclass
class ExportProfile:
    """一个出海账号的画面处理配方。

    crop_bottom_ratio:
        裁掉画面底部多少比例（用于去掉国内烧录的中文硬字幕）。
        实测国内素材字幕区集中在下 8%~14%，默认 0.10；
        裁切后需要重新放大回原高度（见 _crop_and_scale_filter）。

    strip_audio:
        True = 剥离全部音轨（搞笑/动画类：中文配音在海外无意义）；
        False = 保留原声（解压类 ASMR 的命脉）。

    watermark_presets:
        ffmpeg delogo 的预置区域名（见 config.PRESET_REGIONS）。
    """

    name: str
    crop_bottom_ratio: float = 0.10
    strip_audio: bool = False
    watermark_presets: tuple[str, ...] = ()

    @classmethod
    def for_account(cls, account: str) -> "ExportProfile":
        """按账号返回默认配方。

        配方来源是四条赛道的实测差异，不是拍脑袋：
        - ig_satisfying：解压类，声音就是内容本身 → **绝不剥音轨**；
          字幕区裁切也保守（0.08），因为手工艺特写的细节常在画面下部；
        - ig_pets：萌宠实拍/拟真动画，多有原声（叫声/环境声）→ 保留音频；
        - ig_anim：3D/AI 动画，常配中文旁白 → **剥音轨**；
        - ig_home：家居展示，多配轻音乐 → 保留（音乐本身是氛围的一部分），
          但若原音是中文口播，仍需人工判断，故默认保留。
        """
        presets = {
            "ig_satisfying": cls("ig_satisfying", crop_bottom_ratio=0.08,
                                 strip_audio=False,
                                 watermark_presets=("corner_pair",)),
            "ig_pets": cls("ig_pets", crop_bottom_ratio=0.10,
                           strip_audio=False,
                           watermark_presets=("corner_pair",)),
            "ig_anim": cls("ig_anim", crop_bottom_ratio=0.12,
                           strip_audio=True,
                           watermark_presets=("corner_pair",)),
            "ig_home": cls("ig_home", crop_bottom_ratio=0.09,
                           strip_audio=False,
                           watermark_presets=("corner_pair",)),
            "youtube": cls("youtube", crop_bottom_ratio=0.10,
                           strip_audio=True,
                           watermark_presets=("corner_pair",)),
        }
        return presets.get(account, cls(account))

    def to_ffmpeg_opts(self) -> dict:
        return {
            "crop_bottom_ratio": self.crop_bottom_ratio,
            "strip_audio": self.strip_audio,
            "watermark_presets": list(self.watermark_presets),
        }


_IG_ACCOUNTS = ("ig_pets", "ig_satisfying", "ig_anim", "ig_home")
_EXPORT_ACCOUNTS = ("ig_pets", "ig_satisfying", "ig_anim", "ig_home", "youtube")


def is_export_task(target_platforms: str | None) -> bool:
    """判断该任务是否是出海任务（目标平台里有 ig_* 或 youtube）。"""
    if not target_platforms:
        return False
    names = {p.strip() for p in target_platforms.split(",") if p.strip()}
    return bool(names & set(_EXPORT_ACCOUNTS))


def export_account_of(target_platforms: str | None) -> str:
    """取出该任务的出海账号名（多个时取第一个出海账号）。"""
    if not target_platforms:
        return ""
    for p in target_platforms.split(","):
        name = p.strip()
        if name in _EXPORT_ACCOUNTS:
            return name
    return ""


def _crop_and_scale_filter(width: int, height: int, crop_bottom: float,
                           rng: random.Random) -> tuple[str, dict]:
    """构造「裁掉底部字幕区 + 轻微缩放」的滤镜链。

    两步的用意：
    1. crop 掉底部中文字幕（含一点随机浮动，避免所有视频切口完全一致）；
    2. **再放大回原尺寸**——这不是多此一举：直接裁会让画面变矮、
       IG 上传后上下补黑边；放大回原比例能保持 9:16 满屏，
       同时裁切+缩放本身也微改了画面尺度（顺带弱化搬运指纹）。
    """
    # 裁切比例加 ±15% 抖动，避免整批视频切口一模一样（同质化判定）
    jitter = rng.uniform(0.85, 1.15)
    ratio = max(0.0, min(0.25, crop_bottom * jitter))
    if ratio <= 0.005:
        return "", {"crop_bottom_ratio": 0.0}

    keep_h = max(2, int(height * (1 - ratio)) // 2 * 2)   # 保持偶数（h264 要求）
    # 裁完再把高度拉回原尺寸，维持原分辨率与 9:16 比例
    vf = (f"crop={width}:{keep_h}:0:0,"
          f"scale={width}:{height}:flags=bicubic")
    return vf, {"crop_bottom_ratio": round(ratio, 4), "keep_h": keep_h}


def build_export_video(src: Path, dst: Path, profile: ExportProfile,
                       seed: int | None = None) -> dict:
    """执行出海画面清洗：裁字幕区 + 可选剥音轨 + 水印擦除。

    返回处理参数（写日志用，便于事后归因「哪条视频效果差是不是处理过头了」）。
    """
    info = ffmpeg.video_info(src)
    width, height = int(info["width"]), int(info["height"])
    rng = random.Random(seed)

    vf, params = _crop_and_scale_filter(width, height,
                                        profile.crop_bottom_ratio, rng)

    cmd = [ffmpeg._ffmpeg(), "-y", "-i", str(src)]  # noqa: SLF001
    if vf:
        cmd += ["-vf", vf]

    # 视频统一重编码（裁切无法 copy），参数取中等质量
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p"]

    if profile.strip_audio or not info.get("has_audio"):
        cmd += ["-an"]
        params["audio"] = "stripped"
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
        params["audio"] = "kept"

    cmd += ["-movflags", "+faststart", str(dst)]
    ffmpeg._run(cmd, timeout=600)  # noqa: SLF001

    # 水印擦除（可选，独立一遍：delogo 需要重编码）
    if profile.watermark_presets:
        try:
            from ..config import PRESET_REGIONS
            regions: list = []
            for name in profile.watermark_presets:
                regions.extend(PRESET_REGIONS.get(name, []))
            if regions:
                ffmpeg.delogo_watermark(dst, dst, regions, box=1)
                params["watermark"] = "removed"
            else:
                params["watermark"] = "no-preset"
        except Exception as e:
            log.warning("出海水印擦除失败（继续）：%s", str(e)[:120])
            params["watermark"] = f"failed: {str(e)[:60]}"

    return params


def verify_export(src: Path, dst: Path) -> list[str]:
    """检查清洗结果是否符合出海基本要求，返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    if not dst.exists() or dst.stat().st_size < 10_000:
        problems.append("输出文件不存在或过小")
        return problems

    try:
        info = ffmpeg.video_info(dst)
    except Exception as e:
        problems.append(f"无法读取输出视频信息：{e}")
        return problems

    w, h = int(info["width"]), int(info["height"])
    # IG Reels 只吃竖屏或方屏；横屏会被裁掉两侧主体
    if w > h:
        problems.append(f"输出仍是横屏 {w}x{h}：IG Reels 需要 9:16 竖屏")

    if h > 1920:
        problems.append(f"高度 {h} 超过 1080p 等效值，可能被平台压缩")

    duration = float(info.get("duration") or 0)
    if duration < 3:
        problems.append(f"时长仅 {duration:.1f}s，Reels 太短难以进入推荐")
    if duration > 90:
        problems.append(f"时长 {duration:.0f}s，Reels 超过 90s 会显著掉完播")

    return problems

"""ffmpeg/ffprobe 封装：转码、封面抽取、字幕烧录。"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# 转码超时：30 分钟上限，足够处理较长的 Reels
TRANSCODE_TIMEOUT = 30 * 60

# 烧字幕需要 libass（ass 滤镜）。Homebrew 新版精简 ffmpeg 不含 libass，
# 会自动探测 ffmpeg@7 / ffmpeg-full 等 keg-only 的完整构建。
_KEG_CANDIDATES = [
    "/opt/homebrew/opt/ffmpeg@7/bin",
    "/opt/homebrew/opt/ffmpeg@6/bin",
    "/opt/homebrew/opt/ffmpeg@5/bin",
    "/opt/homebrew/opt/ffmpeg-full/bin",
    "/usr/local/opt/ffmpeg@7/bin",
    "/usr/local/opt/ffmpeg@6/bin",
]


class FFmpegError(RuntimeError):
    pass


def _find_bin(name: str) -> str:
    # 1) 环境变量显式指定（FFMPEG_PATH 可指向 ffmpeg 或其 bin 目录）
    override = os.environ.get("FFMPEG_PATH", "").strip()
    if override:
        p = Path(override)
        if p.is_dir():
            p = p / name
        if p.exists():
            return str(p)
    # 2) keg-only 完整构建
    for d in _KEG_CANDIDATES:
        p = Path(d) / name
        if p.exists():
            return str(p)
    # 3) PATH
    found = shutil.which(name)
    if found:
        return found
    raise FFmpegError(
        "未找到可用的 ffmpeg/ffprobe。注意：Homebrew 最新版 ffmpeg 是精简构建（无 libass，"
        "无法烧字幕），请安装完整版：brew install ffmpeg@7"
    )


def _ffmpeg() -> str:
    return _find_bin("ffmpeg")


def _ffprobe() -> str:
    return _find_bin("ffprobe")


def _run(cmd: list[str], timeout: int = TRANSCODE_TIMEOUT) -> subprocess.CompletedProcess:
    log.debug("exec: %s", " ".join(str(c) for c in cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-800:]
        raise FFmpegError(f"命令执行失败：{' '.join(str(c) for c in cmd[:6])}...\n{tail}")
    return proc


def video_info(path: Path) -> dict:
    """返回 {width, height, vcodec, has_audio, acodec, duration}。"""
    proc = _run([
        _ffprobe(), "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ], timeout=60)
    import json
    data = json.loads(proc.stdout)
    info = {"width": 0, "height": 0, "vcodec": "", "has_audio": False, "acodec": "", "duration": 0.0}
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and not info["vcodec"]:
            info["vcodec"] = st.get("codec_name", "")
            info["width"] = int(st.get("width", 0) or 0)
            info["height"] = int(st.get("height", 0) or 0)
        elif st.get("codec_type") == "audio":
            info["has_audio"] = True
            if not info["acodec"]:
                info["acodec"] = st.get("codec_name", "")
    try:
        info["duration"] = float(data.get("format", {}).get("duration", 0) or 0)
    except ValueError:
        pass
    return info


# 短视频平台信息流是 9:16。宽于该比例（16:9 / 4:3 / 1:1）必须铺竖屏，
# 否则快手/抖音用首帧当封面时上下黑边被当成「黑封面」。
VERTICAL_W, VERTICAL_H = 720, 1280
LANDSCAPE_ASPECT = 0.70          # w/h > 0.70 视为需要铺竖屏（9:16=0.5625）
BLACK_LUMA_MAX = 18.0            # YAVG 低于此视为黑帧


def is_landscape(width: int, height: int) -> bool:
    """画面宽于竖屏信息流（含 16:9 / 4:3 / 1:1），需要铺 9:16。"""
    if width <= 0 or height <= 0:
        return False
    return (width / height) > LANDSCAPE_ASPECT


def _parse_yavg(text: str) -> float | None:
    """从 ffmpeg signalstats 日志里抠 YAVG。"""
    for line in (text or "").splitlines():
        if "YAVG" not in line:
            continue
        try:
            return float(line.split("YAVG")[-1].split("=")[-1].strip().split()[0])
        except (IndexError, ValueError):
            continue
    return None


def mean_luma(path: Path) -> float | None:
    """读一张图的平均亮度 0~255；读失败返回 None。"""
    if not path.exists():
        return None
    try:
        proc = subprocess.run(
            [_ffmpeg(), "-i", str(path),
             "-vf", "scale=64:64,signalstats,metadata=print:file=-",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        return _parse_yavg((proc.stderr or "") + (proc.stdout or ""))
    except Exception:
        return None


def is_black_frame(path: Path, luma_max: float = BLACK_LUMA_MAX) -> bool:
    """文件过小或平均亮度过低 → 黑帧。"""
    if not path.exists():
        return True
    if path.stat().st_size < 2500:
        return True
    y = mean_luma(path)
    return y is not None and y < luma_max


def _vertical_pad_filter() -> str:
    """模糊铺底 + 原片居中，输出 720x1280。"""
    return (
        f"[0:v]split=2[orig][copy];"
        f"[copy]scale={VERTICAL_W}:{VERTICAL_H}:force_original_aspect_ratio=increase,"
        f"crop={VERTICAL_W}:{VERTICAL_H},boxblur=20:1[bg];"
        f"[orig]scale={VERTICAL_W}:{VERTICAL_H}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p"
    )


def cover_scale_crop_filter(width: int, height: int) -> str:
    """居中铺满后裁成目标尺寸（抖音竖 3:4 / 横 4:3、快手 ≥1280×960）。"""
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1"
    )


def crop_cover(src: Path, dst: Path, width: int, height: int) -> Path:
    """把选优封面裁成平台格子要的比例。失败时原样拷贝。"""
    if not src.exists():
        return dst
    tmp = dst.with_name(dst.stem + ".crop.jpg")
    try:
        _run([
            _ffmpeg(), "-y", "-i", str(src),
            "-vf", cover_scale_crop_filter(width, height),
            "-frames:v", "1", "-q:v", "2", str(tmp),
        ], timeout=60)
        tmp.replace(dst)
    except FFmpegError:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        if src != dst:
            dst.write_bytes(src.read_bytes())
    return dst


# 抖音竖封面 3:4、横封面 4:3；快手官方建议不低于 1280×960
COVER_PORTRAIT = (960, 1280)     # 3:4
COVER_LANDSCAPE = (1280, 960)    # 4:3，同时满足快手 1280×960


def prepare_platform_covers(src: Path) -> dict[str, Path]:
    """从选优封面切出竖 3:4 和横 4:3。缺文件时返回空 dict。"""
    if not src or not src.exists():
        return {}
    portrait = src.with_name(src.stem + "_34.jpg")
    landscape = src.with_name(src.stem + "_43.jpg")
    crop_cover(src, portrait, *COVER_PORTRAIT)
    crop_cover(src, landscape, *COVER_LANDSCAPE)
    out: dict[str, Path] = {}
    if portrait.exists():
        out["portrait"] = portrait
    if landscape.exists():
        out["landscape"] = landscape
    return out


def pad_image_to_vertical(src: Path, dst: Path) -> Path:
    """把封面图铺成 9:16（模糊铺底），横图不再上下留黑边。"""
    if not src.exists():
        return dst
    tmp = dst.with_name(dst.stem + ".pad.jpg")
    try:
        _run([
            _ffmpeg(), "-y", "-i", str(src),
            "-filter_complex", _vertical_pad_filter(),
            "-frames:v", "1", "-q:v", "2", str(tmp),
        ], timeout=60)
        tmp.replace(dst)
    except FFmpegError:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        if src != dst:
            dst.write_bytes(src.read_bytes())
    return dst


def ensure_compatible(src: Path, dst: Path) -> Path:
    """确保视频为快手网页上传最稳的 H.264/AAC + faststart，分辨率不高于 1080p。

    横屏（YouTube 16:9 等）会铺成 720x1280 竖屏（模糊铺底 + 原片居中），
    避免平台拿片头当封面时出现整块黑边。已兼容的竖屏仅 remux。幂等。
    """
    if dst.exists():
        return dst
    info = video_info(src)
    too_tall = info["height"] > 1080
    bad_video = info["vcodec"] not in ("h264", "avc")
    bad_audio = info["has_audio"] and info["acodec"] not in ("aac",)
    need_pad = is_landscape(info["width"], info["height"])
    tmp = dst.with_name(dst.stem + ".tmp.mp4")

    if need_pad:
        cmd = [
            _ffmpeg(), "-y", "-i", str(src),
            "-filter_complex", _vertical_pad_filter(),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        ]
        if info["has_audio"]:
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", str(tmp)]
        mode = "竖屏铺底"
    elif not (too_tall or bad_video or bad_audio):
        cmd = [_ffmpeg(), "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(tmp)]
        mode = "remux"
    else:
        cmd = [_ffmpeg(), "-y", "-i", str(src)]
        if too_tall or bad_video:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
            if too_tall:
                cmd += ["-vf", "scale='min(1080,iw)':-2"]
        else:
            cmd += ["-c:v", "copy"]
        if info["has_audio"]:
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        cmd += ["-movflags", "+faststart", str(tmp)]
        mode = "转码"

    _run(cmd)
    tmp.rename(dst)
    log.info("视频处理完成 %s（%dx%d → %s）", dst.name, info["width"], info["height"], mode)
    return dst


def strip_metadata(src: Path, dst: Path) -> Path:
    """去除视频元数据里的作者/来源/标签等痕迹（IG 视频会带 title、comment 标签）。

    原地覆盖：dst 指向同一路径时先写临时文件再替换。
    """
    if not src.exists():
        return src
    tmp = dst.with_name(dst.stem + "_clean.mp4") if dst == src else dst
    _run([
        _ffmpeg(), "-y", "-i", str(src),
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-c", "copy", "-movflags", "+faststart", str(tmp),
    ], timeout=300)
    if dst == src:
        tmp.replace(dst)
    return dst


def extract_cover(src: Path, dst: Path, at: float = 1.0) -> Path:
    """抽封面；片头黑帧时向后探测，抽到非黑帧为止。"""
    if dst.exists() and not is_black_frame(dst):
        return dst
    info = video_info(src) if src.exists() else {}
    duration = float(info.get("duration") or 0) or 30.0
    # 优先用户指定点，再 10%/25%/50%，最后才 0s
    candidates = [at, duration * 0.10, duration * 0.25, duration * 0.50, 2.0, 0.5, 0.0]
    seen: set[float] = set()
    last_ok: Path | None = None
    for t in candidates:
        t = max(0.0, min(t, max(duration - 0.05, 0.0)))
        key = round(t, 2)
        if key in seen:
            continue
        seen.add(key)
        try:
            _run([_ffmpeg(), "-y", "-ss", str(t), "-i", str(src),
                  "-frames:v", "1", "-q:v", "2", str(dst)], timeout=120)
        except FFmpegError:
            continue
        if dst.exists():
            last_ok = dst
            if not is_black_frame(dst):
                return dst
    if last_ok is not None:
        return last_ok
    raise FFmpegError(f"无法从 {src.name} 抽取封面")


def burn_subtitles(src: Path, ass_path: Path, dst: Path) -> Path:
    """将 ASS 字幕硬烧进视频。"""
    if dst.exists():
        return dst
    tmp = dst.with_name(dst.stem + ".tmp.mp4")
    # 滤镜参数里的路径需转义 ':' 与 '\''，路径含特殊字符时才需要
    esc = str(ass_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    vf = f"ass={esc}"
    # macOS 的 Homebrew fontconfig 不索引系统字体，需显式指定字体目录，
    # 否则中文字幕会渲染成豆腐块
    if Path("/System/Library/Fonts").is_dir():
        vf += ":fontsdir=/System/Library/Fonts"
    _run([
        _ffmpeg(), "-y", "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart", str(tmp),
    ])
    tmp.rename(dst)
    log.info("字幕烧录完成 %s", dst.name)
    return dst


# ---------- 水印擦除 ----------

# 预置水印区域：坐标为 (x, y, w, h) 四元组。
# 支持两种写法：
#   1) 绝对像素：(100, 50, 200, 80)
#   2) 相对比例：(0.03, 0.03, 0.12, 0.06) —— 默认，自动按视频尺寸换算，
#      换分辨率/换素材都不用改配置。
def _to_pixels(region: tuple, width: int, height: int) -> tuple[int, int, int, int]:
    """水印区域统一换算成像素。数值全 <=1 视为相对比例。"""
    x, y, w, h = (float(v) for v in region)
    if all(v <= 1.0 for v in (x, y, w, h)):
        x, y, w, h = x * width, y * height, w * width, h * height
    # delogo 要求区域完全落在画面内，且至少要 1x1
    px, py = int(max(0, min(x, width - 2))), int(max(0, min(y, height - 2)))
    pw = int(max(2, min(w, width - px - 1)))
    ph = int(max(2, min(h, height - py - 1)))
    return px, py, pw, ph


_DELOGO_SUPPORTS_BOX: bool | None = None


def _delogo_supports_box() -> bool:
    """探测当前 ffmpeg 的 delogo 滤镜是否支持 box 参数。

    不同发行版差异很大：Debian/Ubuntu 的 ffmpeg 长期只有 x/y/w/h/show，
    带 box 的版本传该参数会直接报 "Option 'box' not found" 并中止转码。
    结果缓存，只探测一次。
    """
    global _DELOGO_SUPPORTS_BOX
    if _DELOGO_SUPPORTS_BOX is None:
        try:
            proc = subprocess.run([_ffmpeg(), "-hide_banner", "-h", "filter=delogo"],
                                  capture_output=True, text=True, timeout=20)
            _DELOGO_SUPPORTS_BOX = "box" in (proc.stdout or "")
        except Exception:
            _DELOGO_SUPPORTS_BOX = False
    return _DELOGO_SUPPORTS_BOX


def delogo_watermark(src: Path, dst: Path, regions: list[tuple],
                     box: int = 1) -> Path:
    """擦除烧录在画面像素里的水印（ffmpeg delogo 滤镜，取周边像素插值填充）。

    regions: [(x, y, w, h), ...]，相对比例或绝对像素均可（见 _to_pixels）。
    box: delogo 采样边框宽度，仅在支持该参数的 ffmpeg 构建上生效（自动探测）。

    注意：
    - 只能擦除**固定位置**的水印；位置随帧漂移的水印需先做运动补偿，本函数不处理。
    - delogo 会重编码视频（无法 -c:v copy），因此画质有轻微损失。
    - 区域务必覆盖完整水印，边缘留 2~4px 余量，否则会残留"水印影"。

    原地覆盖：src == dst 时先写临时文件再替换。
    """
    if not src.exists():
        raise FFmpegError(f"待擦水印的视频不存在: {src}")
    if not regions:
        return src

    info = video_info(src)
    width, height = info["width"], info["height"]
    if width <= 0 or height <= 0:
        raise FFmpegError(f"无法获取视频尺寸: {src}")

    supports_box = _delogo_supports_box()
    filters = []
    for region in regions:
        px, py, pw, ph = _to_pixels(tuple(region), width, height)
        f = f"delogo=x={px}:y={py}:w={pw}:h={ph}"
        if supports_box:
            f += f":box={int(box)}"
        filters.append(f)
        log.info("水印擦除区域：x=%d y=%d w=%d h=%d（视频 %dx%d）",
                 px, py, pw, ph, width, height)
    log.debug("delogo 滤镜（box 支持=%s）：%s", supports_box, ",".join(filters))

    tmp = dst.with_name(dst.stem + ".delogo.mp4") if dst == src else dst
    cmd = [_ffmpeg(), "-y", "-i", str(src), "-vf", ",".join(filters)]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
    if info["has_audio"]:
        cmd += ["-c:a", "copy"]
    cmd += ["-movflags", "+faststart", str(tmp)]
    _run(cmd)
    if dst == src:
        tmp.replace(dst)
    log.info("水印擦除完成 %s（%d 个区域）", dst.name, len(regions))
    return dst

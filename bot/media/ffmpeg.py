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


def ensure_compatible(src: Path, dst: Path) -> Path:
    """确保视频为快手网页上传最稳的 H.264/AAC + faststart，分辨率不高于 1080p。

    已兼容时仅 remux（秒级），否则转码。幂等：dst 已存在直接返回。
    """
    if dst.exists():
        return dst
    info = video_info(src)
    too_tall = info["height"] > 1080
    bad_video = info["vcodec"] not in ("h264", "avc")
    bad_audio = info["has_audio"] and info["acodec"] not in ("aac",)
    tmp = dst.with_name(dst.stem + ".tmp.mp4")

    if not (too_tall or bad_video or bad_audio):
        cmd = [_ffmpeg(), "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(tmp)]
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
    _run(cmd)
    tmp.rename(dst)
    log.info("视频处理完成 %s（%dx%d, %s）", dst.name, info["width"], info["height"],
             "remux" if not (too_tall or bad_video or bad_audio) else "转码")
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
    if dst.exists():
        return dst
    for t in (at, 0.0):
        try:
            _run([_ffmpeg(), "-y", "-ss", str(t), "-i", str(src),
                  "-frames:v", "1", "-q:v", "2", str(dst)], timeout=120)
            return dst
        except FFmpegError:
            continue
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

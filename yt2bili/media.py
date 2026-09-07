from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from PIL import Image

from yt2bili.exceptions import Yt2BiliError

logger = logging.getLogger(__name__)


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise Yt2BiliError(
            "未找到 ffmpeg/ffprobe。请安装 FFmpeg 并加入 PATH 后再试。"
        )


def probe_brief(path: Path) -> dict | None:
    if not path.is_file() or path.stat().st_size <= 0:
        return None
    require_ffmpeg()
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,size",
        "-show_entries",
        "stream=codec_type,codec_name,width,height",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    data = json.loads(result.stdout or "{}")
    streams = list(data.get("streams") or [])
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = None
    try:
        duration = float(fmt.get("duration") or 0) or None
    except (TypeError, ValueError):
        duration = None
    width = int(video["width"]) if video and video.get("width") else None
    height = int(video["height"]) if video and video.get("height") else None
    return {
        "has_video": video is not None,
        "has_audio": audio is not None,
        "duration": duration,
        "width": width,
        "height": height,
        "vcodec": (video or {}).get("codec_name"),
    }


def duration_looks_complete(probed: float | None, expected: int | None) -> bool:
    if probed is None or probed <= 1:
        return False
    if expected is None or expected <= 0:
        return True
    tolerance = max(2.0, expected * 0.02)
    return abs(probed - expected) <= tolerance


def mux_video_audio(video: Path, audio: Path, dest: Path) -> Path:
    require_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp.mp4")
    tmp.unlink(missing_ok=True)
    _run_ffmpeg(
        [
            "-i",
            str(video),
            "-i",
            str(audio),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            str(tmp),
        ]
    )
    if not tmp.is_file() or tmp.stat().st_size <= 0:
        tmp.unlink(missing_ok=True)
        raise Yt2BiliError("合并音视频失败。")
    tmp.replace(dest)
    return dest


def ensure_bilibili_mp4(source: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        logger.info("已存在 video.mp4，跳过转换：%s", dest)
        return dest

    if not source.is_file() or source.stat().st_size <= 0:
        raise Yt2BiliError(f"源视频无效：{source}")

    if source.suffix.lower() == ".mp4" or source.name.lower().endswith(".mp4.part"):
        if source.resolve() != dest.resolve():
            logger.info("源文件已是 MP4，跳过转换：%s", source)
            shutil.copy2(source, dest)
        return dest

    require_ffmpeg()
    streams = _probe_streams(source)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise Yt2BiliError("源文件里没有视频轨道。")

    logger.info("源文件不是 MP4（%s），正在转换为 MP4。", source.suffix)
    cmd = [
        "-i",
        str(source),
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-map",
        "0:v:0",
    ]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-map", "0:a:0"]
    else:
        logger.warning("源视频没有音轨，将只上传画面。")
        cmd += ["-an"]
    cmd.append(str(dest))
    _run_ffmpeg(cmd)

    if not dest.is_file() or dest.stat().st_size <= 0:
        raise Yt2BiliError("转码后的视频文件无效。")
    return dest


def process_cover(src: Path, dest: Path, width: int, height: int) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        logger.info("已存在封面，跳过处理：%s", dest)
        return dest
    try:
        with Image.open(src) as image:
            image = image.convert("RGB")
            image = _center_crop_16x9(image)
            image = image.resize((width, height), Image.Resampling.LANCZOS)
            image.save(dest, format="JPEG", quality=85, optimize=True)
    except Exception as exc:
        raise Yt2BiliError(f"封面处理失败：{exc}") from exc
    return dest


def extract_frame_cover(video: Path, dest: Path) -> Path:
    require_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "-ss",
            "3",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(dest),
        ]
    )
    if not dest.is_file() or dest.stat().st_size <= 0:
        raise Yt2BiliError("无法从视频抽帧作为封面。")
    return dest


def _center_crop_16x9(image: Image.Image) -> Image.Image:
    w, h = image.size
    target = 16 / 9
    current = w / h if h else target
    if abs(current - target) < 0.01:
        return image
    if current > target:
        new_w = int(h * target)
        left = (w - new_w) // 2
        return image.crop((left, 0, left + new_w, h))
    new_h = int(w / target)
    top = (h - new_h) // 2
    return image.crop((0, top, w, top + new_h))


def _probe_streams(path: Path) -> list[dict]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,pix_fmt",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Yt2BiliError(f"ffprobe 无法读取视频：{exc}") from exc
    data = json.loads(result.stdout or "{}")
    return list(data.get("streams") or [])


def _run_ffmpeg(args: list[str]) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats", *args]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise Yt2BiliError("ffmpeg 失败，请查看上方输出。")

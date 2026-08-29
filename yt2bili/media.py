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


def ensure_bilibili_mp4(source: Path, dest: Path) -> Path:
    require_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        logger.info("已存在转码结果，跳过：%s", dest)
        return dest

    streams = _probe_streams(source)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise Yt2BiliError("源文件里没有视频轨道。")

    vcodec = (video.get("codec_name") or "").lower()
    pix_fmt = (video.get("pix_fmt") or "").lower()
    acodec = (audio.get("codec_name") or "").lower() if audio else ""
    can_copy = (
        source.suffix.lower() in {".mp4", ".m4v"}
        and vcodec in {"h264", "avc1"}
        and pix_fmt in {"yuv420p", "yuvj420p"}
        and acodec in {"aac", "mp4a"}
    )

    if can_copy:
        logger.info("视频已是 H.264 + AAC，仅封装为 MP4。")
        _run_ffmpeg(
            [
                "-i",
                str(source),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(dest),
            ]
        )
    else:
        logger.info("正在转码为 B 站兼容的 H.264 + AAC MP4（可能较慢）。")
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

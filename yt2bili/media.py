from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image

from yt2bili.exceptions import InvalidMediaError, Yt2BiliError
from yt2bili import events, process_manager
from yt2bili.tools import find_tool

logger = logging.getLogger(__name__)
_validated: dict[tuple, dict] = {}
_VALIDATION_VERSION = 1


def ffmpeg_tool(name: str) -> str:
    return find_tool(name)


def require_ffmpeg() -> None:
    if any(shutil.which(ffmpeg_tool(name)) is None for name in ("ffmpeg", "ffprobe")):
        raise Yt2BiliError(
            "未找到 ffmpeg/ffprobe。请安装 FFmpeg 并加入 PATH 后再试。"
        )


def _positive_float(value) -> float | None:
    try:
        number = float(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def probe_brief(path: Path) -> dict | None:
    if not path.is_file() or path.stat().st_size <= 0:
        return None
    require_ffmpeg()
    cmd = [
        ffmpeg_tool("ffprobe"),
        "-v",
        "error",
        "-show_entries",
        "format=duration,size,format_name",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,duration,pix_fmt,color_transfer,bit_rate",
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
            **process_manager.creation_options(),
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    if result.stderr.strip():
        logger.warning("探测媒体失败 %s：%s", path.name, result.stderr.strip()[-1000:])
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
        "format_name": fmt.get("format_name", ""),
        "width": width,
        "height": height,
        "vcodec": (video or {}).get("codec_name"),
        "acodec": (audio or {}).get("codec_name"),
        "pix_fmt": (video or {}).get("pix_fmt"),
        "color_transfer": (video or {}).get("color_transfer"),
        "video_duration": _positive_float((video or {}).get("duration")),
        "audio_duration": _positive_float((audio or {}).get("duration")),
        "audio_bitrate": _positive_float((audio or {}).get("bit_rate")),
    }


def duration_looks_complete(probed: float | None, expected: int | float | None) -> bool:
    if probed is None or probed <= 1:
        return False
    if expected is None or expected <= 0:
        return True
    tolerance = max(2.0, min(5.0, expected * 0.002))
    return abs(probed - expected) <= tolerance


def _cuda_decode_args(info: dict) -> list[str]:
    """Try supported NVIDIA decoders by default; callers handle CPU fallback."""
    if (
        os.getenv("YT2BILI_HWACCEL", "auto").strip().lower() not in {"auto", "cuda", ""}
        or info["pix_fmt"] != "yuv420p"
        or info.get("color_transfer") in {"smpte2084", "arib-std-b67"}
    ):
        return []
    decoder = {"av1": "av1_cuvid", "h264": "h264_cuvid", "vp9": "vp9_cuvid"}.get(info["vcodec"])
    return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-c:v", decoder] if decoder else []


def _file_state(path: Path) -> tuple:
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino)


def _validation_fingerprint(path: Path, expected_duration) -> dict:
    # Hash the whole file, not just its head/tail: same-size middle corruption
    # must invalidate even a cache hit with a restored modification timestamp.
    digest = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    last_done = 0
    last_report = time.monotonic()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            events.check_cancelled()
            digest.update(chunk)
            done += len(chunk)
            now = time.monotonic()
            if now - last_report >= 1:
                events.progress("hashing", force=True, percent=min(100, done / total * 100) if total else None,
                                bytes=done, total=total, speed=(done - last_done) / (now - last_report))
                last_done, last_report = done, now
    if done != last_done:
        now = time.monotonic()
        elapsed = now - last_report
        events.progress("hashing", force=True, percent=100 if total else None, bytes=done, total=total,
                        speed=(done - last_done) / elapsed if elapsed > 0 else None)
    tool_states = []
    for name in ("ffmpeg", "ffprobe"):
        tool = Path(shutil.which(ffmpeg_tool(name)) or ffmpeg_tool(name)).resolve()
        tool_states.append([str(tool), list(_file_state(tool))])
    return {
        "version": _VALIDATION_VERSION,
        "path": str(path.resolve()),
        "sha256": digest.hexdigest(),
        "state": list(_file_state(path)),
        "expected_duration": expected_duration,
        "tools": tool_states,
        "hwaccel": os.getenv("YT2BILI_HWACCEL", "auto").strip().lower(),
    }


def _validation_cache_path(path: Path) -> Path:
    return path.with_name(path.name + ".validation.json")


def _read_validation_cache(path: Path, fingerprint: dict) -> bool:
    try:
        record = json.loads(_validation_cache_path(path).read_text(encoding="utf-8"))
        return isinstance(record, dict) and record.get("passed") is True and record.get("fingerprint") == fingerprint
    except (OSError, ValueError):
        return False


def _write_validation_cache(path: Path, fingerprint: dict) -> None:
    tmp = None
    try:
        # Unique file + atomic replace: interrupted/concurrent writes cannot
        # leave a half-written success record that another worker trusts.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".validation-", suffix=".tmp", delete=False) as stream:
            tmp = Path(stream.name)
            json.dump({"passed": True, "fingerprint": fingerprint}, stream)
        tmp.replace(_validation_cache_path(path))
    except OSError as exc:
        logger.warning("校验已通过，但无法保存缓存（下次会重新校验）：%s", exc)
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _decode_track(path: Path, stream: str, expected, acceleration: list[str]) -> float:
    backend = "GPU" if acceleration else "CPU"

    def report(actual: float, speed_ratio: float | None) -> None:
        events.progress("validating", percent=min(100, actual / expected * 100) if expected else None,
                        track=stream, seconds=actual, backend=backend, speed_ratio=speed_ratio)
        percent = f"{min(100.0, actual / expected * 100):.1f}%" if expected else "未知总时长"
        speed_text = f"，{speed_ratio:.2f}x" if speed_ratio is not None else ""
        logger.info("校验进度 [%s] %s %s %s：%.1f / %s 秒（%s%s）",
                    path.parent.name, path.name, stream, backend, actual, expected or "?", percent, speed_text)

    cmd = [
        ffmpeg_tool("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error", "-xerror",
        "-err_detect", "explode", *acceleration, "-i", str(path), "-map", f"0:{stream}",
        "-progress", "pipe:1", "-stats_period", "1", "-nostats", "-f", "null", "-",
    ]
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8", errors="replace", **process_manager.creation_options())
        watcher = process_manager.watch(process)
        watcher.__enter__()
        actual = 0.0
        last_report = time.monotonic()
        last_actual = 0.0
        last_speed = None
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if not line.startswith("out_time_us="):
                    continue
                value = line.strip().split("=", 1)[1]
                if not value.lstrip("-").isdigit():
                    continue
                actual = max(actual, int(value) / 1_000_000)
                now = time.monotonic()
                if now - last_report >= 1:
                    last_speed = max(0.0, (actual - last_actual) / (now - last_report))
                    report(actual, last_speed)
                    last_actual, last_report = actual, now
            returncode = process.wait()
        finally:
            watcher.__exit__(None, None, None)
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
        errors.seek(0)
        detail = errors.read(4000).decode("utf-8", errors="replace").strip()
    events.check_cancelled()
    if returncode or detail:
        raise InvalidMediaError(f"{path.name} 的 {stream} 解码失败：{detail or returncode}")
    if not duration_looks_complete(actual, expected):
        raise InvalidMediaError(f"{path.name} 的 {stream} 实际仅 {actual:.2f}s，预期 {expected}s；需要重新下载。")
    report(actual, last_speed)
    return actual


def validate_media(
    path: Path, expected_duration: int | float | None = None, *, require_audio: bool = True
) -> dict:
    """Decode each track: container duration can hide a truncated video track."""
    if not path.is_file() or path.stat().st_size == 0:
        raise InvalidMediaError(f"媒体文件不存在或为空：{path}")
    initial_state = _file_state(path)
    info = probe_brief(path)
    if not info or not info["has_video"] or (require_audio and not info["has_audio"]):
        raise InvalidMediaError(f"媒体损坏或缺少音视频轨道：{path}")
    expected = expected_duration or info["duration"]
    # Cheap rejection before decoding large files; never trust this alone.
    for field in ("video_duration", "audio_duration"):
        duration = info[field]
        if duration and not duration_looks_complete(duration, expected):
            raise InvalidMediaError(f"{path.name} 的 {field}={duration:.2f}s，预期 {expected}s；需要重新下载。")
    cache_enabled = os.getenv("YT2BILI_VALIDATION_CACHE", "1").strip().lower() not in {"0", "off", "false"}
    fingerprint = None
    key = None
    if cache_enabled:
        logger.info("检查完整文件指纹及校验缓存：%s", path.name)
        fingerprint = _validation_fingerprint(path, expected_duration)
        if _file_state(path) != initial_state:
            raise Yt2BiliError(f"计算指纹期间文件发生变化，请停止写入后重试：{path}")
        key = (json.dumps(fingerprint, sort_keys=True),)
        if key in _validated or _read_validation_cache(path, fingerprint):
            if _file_state(path) != initial_state:
                raise Yt2BiliError(f"读取缓存期间文件发生变化，请重试：{path}")
            logger.info("命中完整校验缓存，跳过重复解码：%s", path)
            _validated[key] = info
            return info
    logger.info("正在完整解码校验（不会写出转码文件）：%s", path)
    ends = []
    for stream in (["v:0", "a:0"] if info["has_audio"] else ["v:0"]):
        acceleration = _cuda_decode_args(info) if stream == "v:0" else []
        logger.info("校验 %s %s：%s", path.name, stream, "优先 NVIDIA GPU" if acceleration else "CPU")
        try:
            actual = _decode_track(path, stream, expected, acceleration)
        except InvalidMediaError as exc:
            if not acceleration:
                raise
            logger.warning("GPU 校验不可用或失败，自动从头使用 CPU 复核：%s", exc)
            actual = _decode_track(path, stream, expected, [])
        ends.append(actual)
    if len(ends) == 2 and not duration_looks_complete(ends[0], ends[1]):
        raise InvalidMediaError(f"{path.name} 音视频长度不一致：{ends}，需要重新下载。")
    if _file_state(path) != initial_state:
        raise Yt2BiliError(f"校验期间文件发生变化，不保存校验结果，请重试：{path}")
    if fingerprint is not None:
        _write_validation_cache(path, fingerprint)
        assert key is not None
        _validated[key] = info
    return info


def mux_video_audio(video: Path, audio: Path, dest: Path, expected_duration: int | None = None) -> Path:
    require_ffmpeg()
    validate_media(video, expected_duration, require_audio=False)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp.mp4")
    tmp.unlink(missing_ok=True)
    audio_info = probe_brief(audio)
    audio_args = ["-c:a", "copy"] if audio_info and audio_info["acodec"] == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    try:
        _run_ffmpeg([
            "-xerror",
            "-err_detect",
            "explode",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-c:v",
            "copy",
            *audio_args,
            "-movflags",
            "+faststart",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            str(tmp),
        ])
        validate_media(tmp, expected_duration)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def prepare_upload_video(
    source: Path, dest: Path, expected_duration: int | None = None, *,
    encoder: str = "libx264", max_size_gb: float | None = None,
) -> Path:
    """Upload validated MP4 bytes unchanged; only convert other containers."""
    if max_size_gb is not None and (not math.isfinite(max_size_gb) or max_size_gb <= 0):
        raise Yt2BiliError("文件大小预算必须为正数。")
    info = validate_media(source, expected_duration)
    if source.suffix.lower() == ".mp4" and "mp4" in info["format_name"].split(","):
        if max_size_gb is not None and source.stat().st_size > max_size_gb * 1_000_000_000:
            raise Yt2BiliError("MP4 超过文件大小上限；按直接上传策略不自动压缩，请提高 --max-size-gb 或另行处理。")
        logger.info("MP4 已通过完整校验，直接使用原文件，不转码：%s", source)
        return source
    return ensure_bilibili_mp4(source, dest, expected_duration, encoder=encoder, max_size_gb=max_size_gb)


def ensure_bilibili_mp4(
    source: Path, dest: Path, expected_duration: int | None = None, *, encoder: str = "libx264",
    max_size_gb: float | None = None,
) -> Path:
    if encoder not in {"libx264", "h264_nvenc"}:
        raise Yt2BiliError(f"不支持的视频编码器：{encoder}")
    if max_size_gb is not None and (not math.isfinite(max_size_gb) or max_size_gb <= 0):
        raise Yt2BiliError("文件大小预算必须为正数。")
    max_bytes = int(max_size_gb * 1_000_000_000) if max_size_gb is not None else None
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        try:
            info = validate_media(dest, expected_duration)
            if _upload_compatible(info) and (max_bytes is None or dest.stat().st_size <= max_bytes):
                logger.info("已有投稿文件通过完整性和编码校验：%s", dest)
                return dest
        except InvalidMediaError:
            if source.resolve() == dest.resolve():
                raise

    if not source.is_file() or source.stat().st_size <= 0:
        raise Yt2BiliError(f"源视频无效：{source}")

    info = validate_media(source, expected_duration)
    video_bitrate = None
    if max_bytes is not None:
        duration = expected_duration or info["duration"]
        if not duration:
            raise Yt2BiliError("无法获取时长，不能计算文件大小预算。")
        audio_bitrate = max(320_000, info.get("audio_bitrate") or 0)
        video_bitrate = min(60_000_000, int(max_bytes * 0.96 * 8 / duration - audio_bitrate))
        if video_bitrate < 100_000:
            raise Yt2BiliError("指定的文件大小预算不足，请提高 --max-size-gb。")
        logger.info("文件预算 %.2f GB，视频码率上限 %.2f Mbps。", max_size_gb, video_bitrate / 1_000_000)
    tmp = dest.with_name(dest.stem + ".encoding.tmp.mp4")
    acceleration = _cuda_decode_args(info) if encoder == "h264_nvenc" and info["vcodec"] != "h264" and os.getenv("YT2BILI_HWACCEL") == "cuda" else []
    cmd = ["-xerror", "-err_detect", "explode", *acceleration, "-i", str(source), "-map", "0:v:0", "-map", "0:a:0"]
    if info["vcodec"] == "h264" and info["pix_fmt"] == "yuv420p" and (max_bytes is None or source.stat().st_size <= max_bytes):
        logger.info("视频编码为 H.264，复制视频流：%s", source)
        cmd += ["-c:v", "copy"]
    else:
        logger.info("将 %s/%s 转成 H.264/yuv420p，保留原分辨率。", info["vcodec"], info["pix_fmt"])
        if encoder == "h264_nvenc":
            cmd += ["-c:v", encoder, "-preset", "p4", "-rc", "vbr", "-cq", "18", "-b:v", str(video_bitrate or 0)]
        else:
            cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]
        if not acceleration:
            cmd += ["-pix_fmt", "yuv420p"]
        if video_bitrate:
            cmd += ["-maxrate", str(video_bitrate), "-bufsize", str(video_bitrate * 2)]
        if info["color_transfer"] in {"smpte2084", "arib-std-b67"}:
            cmd += ["-vf", "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"]
    cmd += ["-c:a", "copy"] if info["acodec"] == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", str(tmp)]
    try:
        _run_ffmpeg(cmd)
        checked = validate_media(tmp, expected_duration)
        if not _upload_compatible(checked):
            raise InvalidMediaError("转换后的编码不符合 H.264/AAC 投稿要求。")
        if max_bytes is not None and tmp.stat().st_size > max_bytes:
            raise Yt2BiliError("输出超过文件大小预算，请降低码率后重新生成。")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def _upload_compatible(info: dict) -> bool:
    return info["vcodec"] == "h264" and info["pix_fmt"] == "yuv420p" and info["acodec"] == "aac"


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


def _run_ffmpeg(args: list[str]) -> None:
    cmd = [ffmpeg_tool("ffmpeg"), "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-stats", *args]
    result = process_manager.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise Yt2BiliError("ffmpeg 失败，请查看上方输出。")

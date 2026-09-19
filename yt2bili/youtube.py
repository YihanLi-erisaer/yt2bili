from __future__ import annotations

import json
import logging
import shutil
import sys
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import yt_dlp

from yt2bili import media
from yt2bili.config import Settings
from yt2bili.exceptions import InvalidMediaError, Yt2BiliError

logger = logging.getLogger(__name__)

_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi", ".m4v"}
_SKIP_EXTS = {".json", ".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt", ".nfo", ".part"}
_js_runtime_logged = False
_download_lock = threading.Lock() if sys.platform == "win32" else None
_download_context = threading.local()


@contextmanager
def download_slots(gate):
    """Share a batch's download budget without holding it during validation."""
    previous = getattr(_download_context, "gate", None)
    _download_context.gate = gate
    try:
        yield
    finally:
        _download_context.gate = previous


def _download_with_slot(opts: dict, url: str) -> dict:
    gate = getattr(_download_context, "gate", None)
    logger.info("等待下载名额：%s", url)
    # Include yt-dlp merging, renaming and cookie writes in the critical section.
    # Validation, cache checks and retry backoff must remain outside it.
    with gate if gate is not None else nullcontext():
        with _download_lock if _download_lock is not None else nullcontext():
            with yt_dlp.YoutubeDL(opts) as ydl:
                result = ydl.extract_info(url, download=True)
    logger.info("下载/合并阶段结束，已释放下载名额：%s", url)
    return result


_BOT_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "confirm you’re not a bot",
    "confirm you're not a bot",
)
_RANGE_MARKERS = (
    "416",
    "requested range not satisfiable",
    "range not satisfiable",
)
_FILE_LOCK_MARKERS = (
    "winerror 32",
    "being used by another process",
    "unable to rename file",
    "另一个程序正在使用此文件",
    "进程无法访问",
)
_SSL_MARKERS = (
    "ssl:",
    "unexpected_eof_while_reading",
    "eof occurred in violation of protocol",
)


@dataclass
class YoutubeMeta:
    video_id: str
    url: str
    title: str
    description: str
    uploader: str
    duration: int | None
    thumbnail_url: str
    language: str | None
    webpage_url: str

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> YoutubeMeta:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)


def fetch_meta(url: str, settings: Settings) -> YoutubeMeta:
    info = _extract(url, settings, download=False)
    if info.get("_type") == "playlist":
        raise Yt2BiliError("当前只支持单条视频链接，不支持播放列表或频道。")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise Yt2BiliError("该链接是直播或未开始的直播，已跳过。")

    video_id = info.get("id") or ""
    if not video_id:
        raise Yt2BiliError("无法解析 YouTube 视频 ID。")

    thumb = _best_thumbnail(info)
    webpage = info.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
    return YoutubeMeta(
        video_id=video_id,
        url=url,
        title=(info.get("title") or "").strip(),
        description=(info.get("description") or "").strip(),
        uploader=(info.get("uploader") or info.get("channel") or "").strip(),
        duration=int(info["duration"]) if info.get("duration") is not None else None,
        thumbnail_url=thumb,
        language=info.get("language"),
        webpage_url=webpage,
    )


def download_video(
    url: str,
    work_dir: Path,
    settings: Settings,
    expected_duration: int | None = None,
    *,
    validate: bool = True,
) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    finished = _finish_existing_source(url, work_dir, settings, expected_duration) if validate else _find_source(work_dir)
    if finished:
        return finished

    opts = _base_opts(settings)
    opts.update(
        {
            "outtmpl": str(work_dir / "source.%(ext)s"),
            "format": "bv*+ba/b",
            "format_sort": ["res", "fps", "hdr:12", "vcodec:av01", "vcodec:vp9", "br"],
            "format_sort_force": True,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "overwrites": False,
            "retries": 10,
            "fragment_retries": 10,
            "extractor_retries": 3,
            "skip_unavailable_fragments": False,
        }
    )
    last_error: Exception | None = None
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        finished = _finish_existing_source(url, work_dir, settings, expected_duration) if validate else _find_source(work_dir)
        if finished:
            return finished
        try:
            logger.info("开始下载视频（第 %s/%s 次）", attempt, max_attempts)
            info = _download_with_slot(opts, url)
            _log_selected_format(info)
            source = _find_source(work_dir)
            if source is None:
                raise Yt2BiliError("下载完成但未找到视频文件。")
            if source.stat().st_size <= 0:
                raise Yt2BiliError("下载的视频文件为空。")
            if not validate:
                logger.info("下载完成，等待独立校验队列：%s", source)
                return source
            return _ensure_has_audio(url, work_dir, settings, source, expected_duration)
        except InvalidMediaError as exc:
            last_error = exc
            logger.warning("下载文件校验失败，将隔离并重新下载：%s", exc)
            for path in list(work_dir.glob("source.*")) + list(work_dir.glob("audio.*")):
                if path.is_file():
                    quarantine_file(path)
            if attempt == max_attempts:
                raise
        except Yt2BiliError:
            raise
        except Exception as exc:  # yt-dlp raises DownloadError
            last_error = exc
            logger.warning("下载失败：%s", exc)
            recovered = _finish_existing_source(
                url, work_dir, settings, expected_duration
            ) if validate else None
            if recovered:
                logger.info("本地下载已完整，跳过失败的收尾步骤。")
                return recovered
            if _is_file_lock_error(exc):
                if attempt < max_attempts:
                    wait = 5 * attempt
                    logger.warning(
                        "Windows 文件被占用（常见于杀毒扫描），%s 秒后重试。",
                        wait,
                    )
                    time.sleep(wait)
                    continue
            if _is_range_error(exc):
                recovered = _recover_after_416(
                    url, work_dir, settings, expected_duration
                ) if validate else None
                if not validate:
                    _clear_partial_downloads(work_dir)
                if recovered:
                    return recovered
                if attempt < max_attempts:
                    opts = {**opts, "continuedl": False, "overwrites": True}
                    logger.warning("半成品无法续传（HTTP 416），将从头下载。")
                    time.sleep(2)
                    continue
            if _is_ssl_error(exc) and attempt < max_attempts:
                wait = 10 * attempt
                logger.warning("网络 SSL 连接中断，%s 秒后重试。", wait)
                time.sleep(wait)
                continue
            if _is_bot_block(exc) and attempt < max_attempts:
                wait = 15 * attempt
                logger.warning("YouTube 机器人校验失败，%s 秒后重试。", wait)
                time.sleep(wait)
                continue
            if attempt < max_attempts:
                time.sleep(2 ** attempt)
    raise Yt2BiliError(_format_ytdlp_error("视频下载失败", last_error))


def _finish_existing_source(
    url: str,
    work_dir: Path,
    settings: Settings,
    expected_duration: int | None,
) -> Path | None:
    video = _best_complete_video(work_dir, expected_duration)
    if video is None:
        return None
    try:
        return _ensure_has_audio(url, work_dir, settings, video, expected_duration)
    except InvalidMediaError as exc:
        logger.warning("本地媒体校验失败：%s", exc)
        quarantine_file(video)
        for path in work_dir.glob("audio.*"):
            if path.is_file():
                quarantine_file(path)
        return None


def quarantine_file(path: Path) -> Path:
    """Preserve rejected downloads outside the reusable source glob."""
    folder = path.parent / "rejected" / uuid.uuid4().hex[:12]
    folder.mkdir(parents=True)
    dest = folder / path.name
    path.replace(dest)
    logger.warning("已隔离旧文件，可从此路径恢复：%s", dest)
    return dest


def _recover_after_416(
    url: str,
    work_dir: Path,
    settings: Settings,
    expected_duration: int | None,
) -> Path | None:
    video = _best_complete_video(work_dir, expected_duration)
    if video is None:
        _clear_partial_downloads(work_dir)
        return None
    logger.info("416 续传失败，但本地画面已完整：%s", video.name)
    try:
        return _ensure_has_audio(url, work_dir, settings, video, expected_duration)
    except Exception as exc:
        logger.warning("补音频失败，将隔离半成品后重下：%s", exc)
        _clear_partial_downloads(work_dir)
        return None


def _best_complete_video(
    work_dir: Path,
    expected_duration: int | None,
) -> Path | None:
    candidates: list[Path] = []
    for path in work_dir.glob("source.*"):
        if path.suffix.lower() in {".json", ".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt", ".nfo"}:
            continue
        if path.name.endswith(".ytdl") or ".tmp." in path.name:
            continue
        if path.suffix.lower() == ".part" or path.name.endswith(".mp4.part"):
            # Only yt-dlp knows whether a .part download has finished.
            continue
        if path.suffix.lower() in _VIDEO_EXTS:
            candidates.append(path)
    best: tuple[float, Path] | None = None
    for path in candidates:
        if path.stat().st_size <= 0:
            continue
        try:
            info = media.validate_media(path, expected_duration, require_audio=False)
        except InvalidMediaError as exc:
            logger.warning("不复用无效源文件：%s", exc)
            quarantine_file(path)
            continue
        duration = info.get("duration")
        score = float(duration or 0) + (1_000_000 if info.get("has_audio") else 0)
        if best is None or score > best[0]:
            best = (score, path)
    if best is None:
        return None
    return best[1]


def _ensure_has_audio(
    url: str,
    work_dir: Path,
    settings: Settings,
    video: Path,
    expected_duration: int | None,
) -> Path:
    dest = work_dir / "source.mp4"
    info = media.validate_media(video, expected_duration, require_audio=False)
    if info and info.get("has_audio") and info.get("has_video"):
        logger.info("已存在带音轨的源视频：%s", video)
        return video

    logger.info("画面已完整但没有音轨，开始补下载音频。")
    audio = _download_audio_only(url, work_dir, settings)
    merged = media.mux_video_audio(video, audio, dest, expected_duration)
    logger.info("已合并音视频：%s", merged)
    return merged


def _download_audio_only(url: str, work_dir: Path, settings: Settings) -> Path:
    existing = _find_audio(work_dir)
    if existing:
        logger.info("已存在音轨，跳过下载：%s", existing)
        return existing
    opts = _base_opts(settings)
    opts.update(
        {
            "outtmpl": str(work_dir / "audio.%(ext)s"),
            "format": "ba/bestaudio/b",
            "noplaylist": True,
            "overwrites": True,
            "retries": 10,
            "fragment_retries": 10,
            "extractor_retries": 5,
        }
    )
    last_error: Exception | None = None
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        existing = _find_audio(work_dir)
        if existing:
            logger.info("已存在音轨，跳过下载：%s", existing)
            return existing
        try:
            logger.info("开始下载音轨（第 %s/%s 次）", attempt, max_attempts)
            _download_with_slot(opts, url)
            audio = _find_audio(work_dir)
            if audio is None:
                raise Yt2BiliError("音频下载完成但未找到音轨文件。")
            return audio
        except Yt2BiliError:
            raise
        except Exception as exc:
            last_error = exc
            logger.warning("音轨下载失败：%s", exc)
            if _is_ssl_error(exc) and attempt < max_attempts:
                wait = 10 * attempt
                logger.warning("网络 SSL 连接中断，%s 秒后重试音轨下载。", wait)
                time.sleep(wait)
                continue
            if attempt < max_attempts:
                time.sleep(2 ** attempt)
    raise Yt2BiliError(_format_ytdlp_error("音轨下载失败", last_error))


def _find_audio(work_dir: Path) -> Path | None:
    found: list[Path] = []
    for path in work_dir.glob("audio.*"):
        if path.suffix.lower() in {".part", ".ytdl", ".json"}:
            continue
        if path.stat().st_size > 0:
            found.append(path)
    if not found:
        return None
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0]


def download_thumbnail(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 yt2bili"})
    try:
        with urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
    except Exception as exc:
        raise Yt2BiliError(f"封面下载失败：{exc}") from exc
    if dest.stat().st_size <= 0:
        raise Yt2BiliError("封面文件为空。")


def describe_js_runtimes(settings: Settings) -> str:
    runtimes = _js_runtimes(settings.bin_dir)
    if not runtimes:
        return ""
    parts = []
    for name, spec in runtimes.items():
        path = spec.get("path") or name
        parts.append(f"{name} ({path})")
    return "；".join(parts)


def export_browser_cookies(settings: Settings, browser: str | None = None) -> Path:
    dest = settings.youtube_cookies or (settings.root / "secrets" / "youtube_cookies.txt")
    dest.parent.mkdir(parents=True, exist_ok=True)
    browsers = [browser] if browser else ["edge", "chrome", "firefox"]
    last_error: Exception | None = None
    for name in browsers:
        if not name:
            continue
        logger.info("尝试从 %s 导出 YouTube cookies（请先完全退出该浏览器）…", name)
        opts = _base_opts(settings)
        opts.pop("cookiefile", None)
        opts["cookiesfrombrowser"] = _parse_browser(name)
        opts["cookiefile"] = str(dest)
        opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(
                    "https://www.youtube.com/watch?v=jNQXAC9IVRw",
                    download=False,
                )
        except Exception as exc:
            last_error = exc
            logger.warning("%s：%s", name, exc)
            if dest.is_file() and dest.stat().st_size <= 0:
                dest.unlink(missing_ok=True)
            continue
        if dest.is_file() and dest.stat().st_size > 0:
            logger.info("已写入 %s", dest)
            return dest
    text = str(last_error or "")
    if "DPAPI" in text or "decrypt" in text.lower():
        raise Yt2BiliError(
            "无法直接读取 Edge/Chrome 的 cookies：新版浏览器用了应用绑定加密，"
            "yt-dlp 在 Windows 上解不开（Failed to decrypt with DPAPI）。\n"
            "这和有没有退出浏览器无关。请改用扩展导出：\n"
            "1. 用 Edge 打开并登录 YouTube\n"
            "2. 安装扩展 Get cookies.txt LOCALLY\n"
            "3. 在 youtube.com 页面导出 Netscape 格式，保存为：\n"
            f"   {dest}\n"
            "4. 再重新 run 刚才的链接（脚本会自动读取该文件）。\n"
            "也可以装 Firefox、在里面登录 YouTube，然后运行：\n"
            "  python -m yt2bili youtube-cookies --browser firefox"
        ) from last_error
    raise Yt2BiliError(
        "无法从浏览器导出 YouTube cookies。\n"
        "若提示无法复制 cookie 数据库，请先完全退出 Edge/Chrome 再试。\n"
        "若是 DPAPI 解密失败，请用扩展导出到 secrets/youtube_cookies.txt。\n"
        f"原始错误：{last_error}"
    )


def _extract(url: str, settings: Settings, download: bool) -> dict[str, Any]:
    opts = _base_opts(settings)
    opts["skip_download"] = not download
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=download)
    except Exception as exc:
        raise Yt2BiliError(_format_ytdlp_error("解析 YouTube 链接失败", exc)) from exc
    if not info:
        raise Yt2BiliError("解析 YouTube 链接失败：没有返回信息。")
    return info


def _base_opts(settings: Settings) -> dict[str, Any]:
    runtimes = _js_runtimes(settings.bin_dir)
    global _js_runtime_logged
    if not _js_runtime_logged:
        _js_runtime_logged = True
        if not runtimes:
            logger.warning(
                "未找到 Deno 或 Node.js。YouTube 解析可能失败。"
                "可安装 Node.js，或把 deno.exe 放到 bin\\。"
            )
        else:
            logger.info("YouTube JS 运行时：%s", describe_js_runtimes(settings))

    opts: dict[str, Any] = {
        "quiet": False,
        "progress_delta": 2,
        "no_warnings": False,
        "noplaylist": True,
        "ignoreerrors": False,
        "skip_unavailable_fragments": False,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 5,
        "socket_timeout": 30,
        "extractor_args": {
            "youtube": {
                # tv/web_safari 常被限制在 1080p HLS；web_embedded 才能拿到 1440/2160 DASH。
                "player_client": ["web_embedded", "tv", "web_safari"],
            }
        },
    }
    if runtimes:
        opts["js_runtimes"] = runtimes
    ffmpeg = Path(media.ffmpeg_tool("ffmpeg"))
    if ffmpeg.is_file():
        opts["ffmpeg_location"] = str(ffmpeg.parent)

    if sys.platform == "win32":
        # Parallel fragment writes + AV scanning often cause WinError 32 on rename.
        opts["concurrent_fragment_downloads"] = 1

    cookie_file = settings.youtube_cookies
    browser = settings.youtube_cookies_from_browser
    if cookie_file:
        if not cookie_file.is_file():
            raise Yt2BiliError(f"YouTube cookies 文件不存在：{cookie_file}")
        opts["cookiefile"] = str(cookie_file)
        logger.info("使用 YouTube cookies 文件：%s", cookie_file)
    elif browser:
        opts["cookiesfrombrowser"] = _parse_browser(browser)
        logger.info(
            "从浏览器读取 YouTube cookies：%s（请先完全退出该浏览器）",
            browser,
        )
    return opts


def _log_selected_format(info: dict[str, Any] | None) -> None:
    if not info:
        return
    requested = info.get("requested_formats")
    items = requested if isinstance(requested, list) else [info]
    for item in items:
        if not isinstance(item, dict):
            continue
        vcodec = item.get("vcodec")
        if vcodec in (None, "none"):
            continue
        height = item.get("height")
        logger.info(
            "下载画质：%sp  format=%s  %s  %s",
            height or "?",
            item.get("format_id"),
            vcodec,
            item.get("ext"),
        )
        protocol = str(item.get("protocol") or "")
        if isinstance(height, int) and height <= 1080 and "m3u8" in protocol:
            logger.warning(
                "当前选中的是 HLS %sp（通常最高 1080p）。"
                "若网页能看 4K，该视频可能禁止嵌入，已无法拿到更高画质。",
                height,
            )


def _js_runtimes(bin_dir: Path) -> dict[str, dict[str, str]]:
    runtimes: dict[str, dict[str, str]] = {}
    deno = _which_exe("deno", bin_dir)
    node = _which_exe("node", bin_dir)
    if deno:
        runtimes["deno"] = {"path": deno}
    if node:
        runtimes["node"] = {"path": node}
    return runtimes


def _which_exe(name: str, bin_dir: Path) -> str | None:
    for candidate in (bin_dir / f"{name}.exe", bin_dir / name):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    return found


def _parse_browser(raw: str) -> tuple[str, ...]:
    text = raw.strip()
    if not text:
        raise Yt2BiliError("YOUTUBE_COOKIES_FROM_BROWSER 为空。")
    if ":" in text:
        browser, profile = text.split(":", 1)
        return (browser.strip().lower(), profile.strip())
    return (text.lower(),)


def _is_range_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _RANGE_MARKERS)


def _is_file_lock_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _FILE_LOCK_MARKERS)


def _is_ssl_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _SSL_MARKERS)


def _clear_partial_downloads(work_dir: Path) -> None:
    removed = 0
    for pattern in ("*.part", "*.ytdl"):
        for path in work_dir.glob(pattern):
            try:
                quarantine_file(path)
                removed += 1
                logger.info("已隔离不完整下载：%s", path.name)
            except OSError as exc:
                logger.warning("隔离 %s 失败：%s", path, exc)
    if removed:
        logger.info("已清理 %s 个半成品文件。", removed)


def _is_bot_block(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _BOT_MARKERS)


def _format_ytdlp_error(prefix: str, exc: BaseException | None) -> str:
    detail = str(exc).strip() if exc else "未知错误"
    if exc is not None and _is_bot_block(exc):
        return (
            f"{prefix}：YouTube 把本次请求当成机器人拦截了。\n"
            "当前 IP 已无法匿名解析，需要已登录 YouTube 的浏览器 cookies。\n"
            "Windows 上新版 Edge/Chrome 无法被 yt-dlp 直接读取，请用扩展导出：\n"
            "1. 用 Edge 打开并登录 YouTube\n"
            "2. 安装 Get cookies.txt LOCALLY，在 youtube.com 导出 Netscape 格式\n"
            "3. 保存为 secrets/youtube_cookies.txt 后重新 run\n"
            f"原始错误：{detail}"
        )
    if exc is not None and _is_range_error(exc):
        return (
            f"{prefix}：断点续传失败（HTTP 416）。已完成的源文件通过校验后才会补音频；半成品会保留到 rejected/ 后重下。\n"
            f"原始错误：{detail}"
        )
    if exc is not None and _is_file_lock_error(exc):
        return (
            f"{prefix}：Windows 上文件被占用，通常是并行下载或杀毒软件扫描导致。\n"
            "可稍等后用 retry 续跑；若反复出现可在 .env 设置 DOWNLOAD_JOBS=1。\n"
            f"原始错误：{detail}"
        )
    if exc is not None and _is_ssl_error(exc):
        return (
            f"{prefix}：下载过程中网络 SSL 连接中断（常见于大文件或网络不稳定）。\n"
            "请用 retry 续传；若仍失败可减小并行数（DOWNLOAD_JOBS=1）或换网络后再试。\n"
            f"原始错误：{detail}"
        )
    return (
        f"{prefix}：{detail}\n"
        "若视频有年龄限制或地区限制，请导出浏览器 cookies 到 secrets/youtube_cookies.txt，"
        "并在 .env 中设置 YOUTUBE_COOKIES。"
    )


def _best_thumbnail(info: dict[str, Any]) -> str:
    thumbs = info.get("thumbnails") or []
    scored: list[tuple[int, str]] = []
    for item in thumbs:
        url = item.get("url")
        if not url:
            continue
        area = int(item.get("width") or 0) * int(item.get("height") or 0)
        scored.append((area, url))
    if scored:
        scored.sort(key=lambda x: x[0])
        return scored[-1][1]
    fallback = info.get("thumbnail") or ""
    if not fallback:
        raise Yt2BiliError("该视频没有可用封面。")
    return fallback


def _find_source(work_dir: Path) -> Path | None:
    found: list[Path] = []
    for path in work_dir.glob("source.*"):
        if path.suffix.lower() in _SKIP_EXTS or ".tmp." in path.name:
            continue
        if path.suffix.lower() in _VIDEO_EXTS and path.stat().st_size > 0:
            found.append(path)
    if not found:
        return None
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0]

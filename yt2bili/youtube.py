from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import yt_dlp

from yt2bili.exceptions import Yt2BiliError

logger = logging.getLogger(__name__)

_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi", ".m4v"}
_SKIP_EXTS = {".json", ".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt", ".nfo", ".part"}


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


def fetch_meta(url: str, cookies: Path | None = None) -> YoutubeMeta:
    info = _extract(url, cookies, download=False)
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


def download_video(url: str, work_dir: Path, cookies: Path | None = None) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    existing = _find_source(work_dir)
    if existing:
        logger.info("已存在源视频，跳过下载：%s", existing)
        return existing

    opts = _base_opts(cookies)
    opts.update(
        {
            "outtmpl": str(work_dir / "source.%(ext)s"),
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "overwrites": False,
            "retries": 3,
            "fragment_retries": 10,
        }
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            logger.info("开始下载视频（第 %s/3 次）", attempt)
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            source = _find_source(work_dir)
            if source is None:
                raise Yt2BiliError("下载完成但未找到视频文件。")
            if source.stat().st_size <= 0:
                raise Yt2BiliError("下载的视频文件为空。")
            return source
        except Yt2BiliError:
            raise
        except Exception as exc:  # yt-dlp raises DownloadError
            last_error = exc
            logger.warning("下载失败：%s", exc)
            time.sleep(2 ** attempt)
    raise Yt2BiliError(f"视频下载失败：{last_error}")


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


def _extract(url: str, cookies: Path | None, download: bool) -> dict[str, Any]:
    opts = _base_opts(cookies)
    opts["skip_download"] = not download
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=download)
    except Exception as exc:
        raise Yt2BiliError(
            f"解析 YouTube 链接失败：{exc}\n"
            "若视频有年龄限制或地区限制，请导出浏览器 cookies 到 secrets/youtube_cookies.txt，"
            "并在 .env 中设置 YOUTUBE_COOKIES。"
        ) from exc
    if not info:
        raise Yt2BiliError("解析 YouTube 链接失败：没有返回信息。")
    return info


def _base_opts(cookies: Path | None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
        "ignoreerrors": False,
    }
    if cookies:
        if not cookies.is_file():
            raise Yt2BiliError(f"YouTube cookies 文件不存在：{cookies}")
        opts["cookiefile"] = str(cookies)
    return opts


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
        if path.suffix.lower() in _SKIP_EXTS:
            continue
        if path.suffix.lower() in _VIDEO_EXTS and path.stat().st_size > 0:
            found.append(path)
    if not found:
        return None
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0]

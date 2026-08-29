from __future__ import annotations

import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from yt2bili import bili_upload, media, translate, youtube
from yt2bili.config import Settings
from yt2bili.db import Task, TaskStore
from yt2bili.exceptions import Yt2BiliError
from yt2bili.youtube import YoutubeMeta

logger = logging.getLogger(__name__)

NOTICE = (
    "本工具只应用于你拥有版权、已获授权，或源平台明确允许转载的视频。"
    "稿件创作声明为「内容无需标注」，简介会保留原标题、原作者和原链接。"
)

_translate_lock = threading.Lock()
_upload_lock = threading.Lock()
_last_upload_monotonic = 0.0
_current = threading.local()
MAX_DOWNLOAD_JOBS = 8


class VideoIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        vid = getattr(_current, "video_id", None)
        if vid:
            record.video_id = vid
            prefix = f"[{vid}] "
            msg = str(record.msg)
            if not msg.startswith(prefix):
                record.msg = prefix + msg
        return True


def install_log_filter() -> None:
    root = logging.getLogger()
    if any(isinstance(item, VideoIdLogFilter) for item in root.filters):
        return
    root.addFilter(VideoIdLogFilter())


def run(
    settings: Settings,
    store: TaskStore,
    url: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> Task:
    media.require_ffmpeg()
    logger.info(NOTICE)
    return _run_one(
        settings,
        store,
        url,
        dry_run=dry_run,
        force=force,
        skip_if_submitted=False,
    )


def run_many(
    settings: Settings,
    store: TaskStore,
    urls: list[str],
    *,
    dry_run: bool = False,
    force: bool = False,
    jobs: int | None = None,
) -> tuple[list[Task], list[tuple[str, str]]]:
    media.require_ffmpeg()
    logger.info(NOTICE)

    unique = _dedupe_urls(urls)
    if not unique:
        raise Yt2BiliError("没有要处理的链接。")

    workers = jobs if jobs is not None else settings.download_jobs
    workers = max(1, min(workers, MAX_DOWNLOAD_JOBS, len(unique)))
    logger.info(
        "批量 %s 条：并行下载 %s 路，B 站上传排队（间隔 %s 秒）。",
        len(unique),
        workers,
        settings.upload_gap_seconds,
    )

    results: list[Task] = []
    failures: list[tuple[str, str]] = []

    def worker(url: str) -> Task:
        return _run_one(
            settings,
            store,
            url,
            dry_run=dry_run,
            force=force,
            skip_if_submitted=True,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(worker, url): url for url in unique}
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                results.append(future.result())
            except Yt2BiliError as exc:
                logger.error("%s 失败：%s", url, exc)
                failures.append((url, str(exc)))
            except Exception as exc:
                logger.exception("%s 未预期错误", url)
                failures.append((url, f"未预期错误：{exc}"))

    logger.info(
        "批量结束：成功 %s，失败 %s。",
        len(results),
        len(failures),
    )
    return results, failures


def retry(
    settings: Settings,
    store: TaskStore,
    video_id: str,
    *,
    dry_run: bool = False,
) -> Task:
    media.require_ffmpeg()
    task = store.require(video_id)
    if task.status == "submitted":
        raise Yt2BiliError(f"{video_id} 已经投稿成功，无需 retry。若要重做请用 run --force。")

    _current.video_id = video_id
    work_dir = Path(task.work_dir) if task.work_dir else settings.work_dir / video_id
    work_dir.mkdir(parents=True, exist_ok=True)
    log_path = work_dir / "pipeline.log"
    _attach_file_handler(log_path, video_id)

    meta_path = work_dir / "meta.json"
    if meta_path.is_file():
        meta = YoutubeMeta.load(meta_path)
    else:
        meta = youtube.fetch_meta(task.url, settings)

    try:
        return _execute(settings, store, task, meta, work_dir, dry_run=dry_run)
    except Yt2BiliError as exc:
        task.status = "failed"
        task.error = str(exc)
        store.upsert(task)
        raise
    except Exception as exc:
        task.status = "failed"
        task.error = f"未预期错误：{exc}"
        store.upsert(task)
        raise Yt2BiliError(task.error) from exc
    finally:
        _detach_file_handler(log_path, video_id)
        _current.video_id = None


def _run_one(
    settings: Settings,
    store: TaskStore,
    url: str,
    *,
    dry_run: bool,
    force: bool,
    skip_if_submitted: bool,
) -> Task:
    meta = youtube.fetch_meta(url, settings)
    _current.video_id = meta.video_id
    log_path: Path | None = None
    try:
        existing = store.get(meta.video_id)
        if existing and existing.status == "submitted" and not force:
            extra = f"（{existing.bv_id}）" if existing.bv_id else ""
            if skip_if_submitted:
                logger.info("跳过已投稿成功的 %s%s", meta.video_id, extra)
                return existing
            raise Yt2BiliError(
                f"{meta.video_id} 已经投稿成功{extra}。若要重做请加 --force。"
            )

        work_dir = settings.work_dir / meta.video_id
        if force and work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        task = existing or Task(video_id=meta.video_id, url=meta.webpage_url, status="pending")
        if force:
            task = Task(video_id=meta.video_id, url=meta.webpage_url, status="pending")
        task.url = meta.webpage_url
        task.work_dir = str(work_dir)
        log_path = work_dir / "pipeline.log"
        _attach_file_handler(log_path, meta.video_id)

        try:
            return _execute(settings, store, task, meta, work_dir, dry_run=dry_run)
        except Yt2BiliError as exc:
            task.status = "failed"
            task.error = str(exc)
            store.upsert(task)
            raise
        except Exception as exc:
            task.status = "failed"
            task.error = f"未预期错误：{exc}"
            store.upsert(task)
            raise Yt2BiliError(task.error) from exc
    finally:
        if log_path is not None:
            _detach_file_handler(log_path, meta.video_id)
        _current.video_id = None


def _execute(
    settings: Settings,
    store: TaskStore,
    task: Task,
    meta: YoutubeMeta,
    work_dir: Path,
    *,
    dry_run: bool,
) -> Task:
    log = logging.getLogger(f"yt2bili.task.{task.video_id}")
    meta.save(work_dir / "meta.json")
    task.title_orig = meta.title
    task.desc_orig = meta.description
    task.uploader = meta.uploader
    task.work_dir = str(work_dir)
    task.error = ""

    task.status = "fetching_meta"
    store.upsert(task)
    log.info("视频：%s | %s", meta.video_id, meta.title)

    video_path = work_dir / "video.mp4"
    task.status = "downloading"
    store.upsert(task)
    if not _ok(video_path):
        source = youtube.download_video(meta.webpage_url, work_dir, settings)
        media.ensure_bilibili_mp4(source, video_path)
    else:
        log.info("已存在 video.mp4，跳过下载/转码。")
    task.video_path = str(video_path)
    store.upsert(task)

    cover_path = work_dir / "cover.jpg"
    task.status = "processing_cover"
    store.upsert(task)
    if not _ok(cover_path):
        raw_cover = work_dir / "thumb_raw"
        try:
            youtube.download_thumbnail(meta.thumbnail_url, raw_cover)
            media.process_cover(
                raw_cover,
                cover_path,
                settings.cover_width,
                settings.cover_height,
            )
        except Yt2BiliError as exc:
            log.warning("封面下载失败，改为从视频抽帧：%s", exc)
            frame = work_dir / "frame.jpg"
            media.extract_frame_cover(video_path, frame)
            media.process_cover(
                frame,
                cover_path,
                settings.cover_width,
                settings.cover_height,
            )
    else:
        log.info("已存在 cover.jpg，跳过封面处理。")
    task.cover_path = str(cover_path)
    store.upsert(task)

    task.status = "translating"
    store.upsert(task)
    if not task.title_zh:
        footer_reserve = 80 + len(meta.title) + len(meta.uploader) + len(meta.webpage_url)
        body_limit = max(200, settings.desc_limit - footer_reserve)
        with _translate_lock:
            title_zh, desc_zh = translate.translate_title_and_desc(
                settings.deepl_auth_key,
                meta.title,
                meta.description,
                meta.language,
                settings.title_limit,
                body_limit,
            )
        task.title_zh = title_zh
        task.desc_zh = translate.build_description(
            desc_zh,
            meta.title,
            meta.uploader,
            meta.webpage_url,
            settings.desc_limit,
        )
        store.upsert(task)
    else:
        log.info("已有中文标题，跳过翻译。")

    (work_dir / "title.txt").write_text(task.title_zh, encoding="utf-8")
    (work_dir / "desc.txt").write_text(task.desc_zh, encoding="utf-8")
    log.info("中文标题：%s", task.title_zh)
    log.info("简介预览：\n%s", task.desc_zh)

    if dry_run:
        task.status = "ready"
        log.info("dry-run：已跳过 B 站上传。")
        store.upsert(task)
        return task

    task.status = "uploading"
    store.upsert(task)
    bv = _upload_serialized(
        settings,
        log,
        video_path,
        cover_path,
        task.title_zh,
        task.desc_zh,
        meta.webpage_url,
    )
    task.bv_id = bv
    task.status = "submitted"
    task.error = ""
    store.upsert(task)
    _cleanup_uploaded_files(settings, work_dir)
    task.work_dir = ""
    task.video_path = ""
    task.cover_path = ""
    store.upsert(task)
    log.info("完成。投稿成功不等于已过审。")
    return task


def _upload_serialized(
    settings: Settings,
    log: logging.Logger,
    video_path: Path,
    cover_path: Path,
    title: str,
    description: str,
    source_url: str,
) -> str:
    global _last_upload_monotonic
    with _upload_lock:
        gap = settings.upload_gap_seconds
        if _last_upload_monotonic > 0 and gap > 0:
            wait = gap - (time.monotonic() - _last_upload_monotonic)
            if wait > 0:
                log.info("上传排队中，等待 %.0f 秒…", wait)
                time.sleep(wait)
        try:
            try:
                bili_upload.renew(settings)
            except Yt2BiliError:
                raise
            except Exception as exc:
                log.warning("刷新登录态时出错，继续投稿：%s", exc)
            return bili_upload.upload(
                settings,
                video_path,
                cover_path,
                title,
                description,
                source_url,
            )
        finally:
            _last_upload_monotonic = time.monotonic()


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for raw in urls:
        url = raw.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


def _ok(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _cleanup_uploaded_files(settings: Settings, current_work_dir: Path) -> None:
    video_id = current_work_dir.name
    _detach_file_handler(current_work_dir / "pipeline.log", video_id)
    _remove_work_dir(settings.work_dir, current_work_dir)


def _remove_work_dir(work_root: Path, target: Path) -> None:
    root = work_root.resolve()
    try:
        resolved = target.resolve()
    except OSError:
        return
    if resolved == root or root not in resolved.parents:
        logger.warning("拒绝删除工作目录之外的路径：%s", resolved)
        return
    if not resolved.exists():
        return
    try:
        shutil.rmtree(resolved)
        logger.info("已删除已投稿文件：%s", resolved)
    except OSError as exc:
        logger.warning("删除 %s 失败：%s", resolved, exc)


def _detach_file_handler(log_path: Path, video_id: str | None = None) -> None:
    try:
        marker = str(log_path.resolve())
    except OSError:
        marker = str(log_path)
    loggers = [logging.getLogger()]
    if video_id:
        loggers.append(logging.getLogger(f"yt2bili.task.{video_id}"))
    for log in loggers:
        for handler in list(log.handlers):
            if getattr(handler, "_yt2bili_path", None) == marker:
                handler.close()
                log.removeHandler(handler)


def _attach_file_handler(log_path: Path, video_id: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(f"yt2bili.task.{video_id}")
    log.setLevel(logging.INFO)
    log.propagate = True
    marker = str(log_path.resolve())
    for handler in log.handlers:
        if getattr(handler, "_yt2bili_path", None) == marker:
            return
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler._yt2bili_path = marker  # type: ignore[attr-defined]
    log.addHandler(handler)

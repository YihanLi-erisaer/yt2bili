from __future__ import annotations

import logging
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from functools import wraps
from dataclasses import dataclass
from pathlib import Path

from yt2bili import bili_upload, media, translate, youtube
from yt2bili.config import Settings
from yt2bili.db import Task, TaskStore
from yt2bili.exceptions import InvalidMediaError, Yt2BiliError
from yt2bili.youtube import YoutubeMeta
from yt2bili import events
from yt2bili.locking import upload_guard, work_lock

logger = logging.getLogger(__name__)

NOTICE = (
    "本工具只应用于你拥有版权、已获授权，或源平台明确允许转载的视频。"
    "稿件创作声明为「内容无需标注」，简介会保留原标题、原作者和原链接。"
)

_translate_lock = threading.Lock()
_upload_lock = threading.Lock()
_last_upload_monotonic = 0.0
_current = threading.local()
MAX_MEDIA_ATTEMPTS = 5


@dataclass
class _PipelineJob:
    url: str
    task: Task | None = None
    meta: YoutubeMeta | None = None
    work_dir: Path | None = None
    source: Path | None = None
    attempts: int = 0
    skipped: bool = False
    lock: object = None


@contextmanager
def _job_context(job: _PipelineJob):
    assert job.task is not None and job.work_dir is not None
    previous = getattr(_current, "video_id", None)
    _current.video_id = job.task.video_id
    log_path = job.work_dir / "pipeline.log"
    _attach_file_handler(log_path, job.task.video_id)
    try:
        yield
    finally:
        _detach_file_handler(log_path, job.task.video_id)
        _current.video_id = previous


def _download_job(settings, store, job: _PipelineJob, force: bool, seen_ids: set[str]):
    if job.task is None:
        meta = youtube.fetch_meta(job.url, settings)
        if meta.video_id in seen_ids:
            job.skipped = True
            logger.info("同一视频的重复链接已跳过：%s", job.url)
            return
        seen_ids.add(meta.video_id)
        existing = store.get(meta.video_id)
        job.meta = meta
        job.work_dir = settings.work_dir / meta.video_id
        if existing and existing.status in ("submitted", "submission_unknown") and not force:
            job.task = existing
            job.skipped = True
            logger.info("跳过已投稿成功的 %s（%s）", meta.video_id, existing.bv_id)
            return
        if events.current_video_id() is None:
            job.lock = work_lock(job.work_dir)
            job.lock.__enter__()
        if force and job.work_dir.exists():
            _remove_work_dir(settings.work_dir, job.work_dir)
            if job.work_dir.exists():
                raise Yt2BiliError(f"无法清理强制重做的任务目录：{job.work_dir}")
        job.work_dir.mkdir(parents=True, exist_ok=True)
        job.task = existing if existing and not force else Task(meta.video_id, meta.webpage_url, "pending")
        job.task.url = meta.webpage_url
        job.task.work_dir = str(job.work_dir)
        job.task.title_orig = meta.title
        job.task.desc_orig = meta.description
        job.task.uploader = meta.uploader
        meta.save(job.work_dir / "meta.json")
    assert job.task is not None and job.meta is not None and job.work_dir is not None
    with _job_context(job):
        job.attempts += 1
        job.task.status = "downloading"
        job.task.error = ""
        store.upsert(job.task)
        video = job.work_dir / "video.mp4"
        # Existing outputs are only candidates: the validation worker is the gatekeeper.
        job.source = video if _ok(video) else youtube.download_video(
            job.meta.webpage_url, job.work_dir, settings,
            expected_duration=job.meta.duration, validate=False,
        )
        job.task.status = "queued_validation"
        store.upsert(job.task)
        logger.info("[%s] 下载阶段完成，已进入校验队列。", job.task.video_id)


def _validate_job(settings, store, job: _PipelineJob):
    assert job.task is not None and job.meta is not None and job.work_dir is not None and job.source is not None
    with _job_context(job):
        job.task.status = "validating"
        store.upsert(job.task)
        video = media.prepare_upload_video(job.source, job.work_dir / "video.mp4", job.meta.duration)
        job.task.video_path = str(video)
        store.upsert(job.task)
        job.task.status = "queued_upload"
        store.upsert(job.task)
        logger.info("[%s] 校验完成，已进入上传队列。", job.task.video_id)


def _upload_job(settings, store, job: _PipelineJob, dry_run: bool):
    assert job.task is not None and job.meta is not None and job.work_dir is not None
    with _job_context(job):
        # Covers/translation must not hold up the next video's validation.
        _prepare_assets(settings, store, job.task, job.meta, job.work_dir, Path(job.task.video_path))
        return _submit_ready(settings, store, job.task, job.meta, job.work_dir, dry_run=dry_run)


def _reject_job_source(job: _PipelineJob):
    assert job.work_dir is not None and job.source is not None
    if job.source == job.work_dir / "video.mp4":
        candidates = [job.source]
    else:
        candidates = list(job.work_dir.glob("source.*")) + list(job.work_dir.glob("audio.*"))
    for path in candidates:
        if path.is_file():
            youtube.quarantine_file(path)
    job.source = None


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

    if jobs not in (None, 1):
        logger.info("三队列模式固定每阶段单路，忽略旧的 -j %s 参数。", jobs)
    logger.info("批量 %s 条：下载、校验处理、上传三个独立单路队列；上传间隔 %s 秒。",
                len(unique), settings.upload_gap_seconds)

    results: list[Task] = []
    failures: list[tuple[str, str]] = []

    seen_ids: set[str] = set()  # accessed only by the single download worker
    pools = {stage: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"yt2bili-{stage}")
             for stage in ("download", "validate", "upload")}
    pending = {}
    batch_jobs = []

    def enqueue(stage, job):
        if stage == "download":
            future = pools[stage].submit(_download_job, settings, store, job, force, seen_ids)
        elif stage == "validate":
            future = pools[stage].submit(_validate_job, settings, store, job)
        else:
            future = pools[stage].submit(_upload_job, settings, store, job, dry_run)
        pending[future] = (stage, job)

    try:
        for url in unique:
            job = _PipelineJob(url)
            batch_jobs.append(job)
            enqueue("download", job)
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in [item for item in pending if item in done]:
                stage, job = pending.pop(future)
                try:
                    future.result()
                    if job.skipped:
                        if job.task is not None:
                            results.append(job.task)
                    elif stage == "download":
                        enqueue("validate", job)
                    elif stage == "validate":
                        enqueue("upload", job)
                    else:
                        results.append(job.task)
                except Exception as exc:
                    if stage == "validate" and isinstance(exc, InvalidMediaError) and job.attempts < MAX_MEDIA_ATTEMPTS:
                        try:
                            _reject_job_source(job)
                            job.task.status = "queued_download"
                            store.upsert(job.task)
                            logger.warning("[%s] 校验失败，退回下载队列（%s/%s）：%s",
                                           job.task.video_id, job.attempts, MAX_MEDIA_ATTEMPTS, exc)
                            enqueue("download", job)
                            continue
                        except Exception as recovery_error:
                            exc = recovery_error
                    logger.error("%s 在 %s 阶段失败：%s", job.url, stage, exc)
                    if job.task is not None:
                        if job.task.status != "submission_unknown":
                            job.task.status = "failed"
                        job.task.error = str(exc)
                        store.upsert(job.task)
                    failures.append((job.url, str(exc)))
    finally:
        for future in pending:
            future.cancel()
        for pool in pools.values():
            pool.shutdown(wait=True, cancel_futures=True)
        for job in batch_jobs:
            if job.lock:
                job.lock.__exit__(None, None, None)

    logger.info(
        "批量结束：成功 %s，失败 %s。",
        len(results),
        len(failures),
    )
    return results, failures


def _lock_existing_task(function):
    @wraps(function)
    def wrapped(settings, store, video_id, *args, **kwargs):
        task = store.require(video_id)
        folder = Path(task.work_dir) if task.work_dir else settings.work_dir / video_id
        with work_lock(folder):
            return function(settings, store, video_id, *args, **kwargs)
    return wrapped


@_lock_existing_task
def retry(
    settings: Settings,
    store: TaskStore,
    video_id: str,
    *,
    dry_run: bool = False,
) -> Task:
    media.require_ffmpeg()
    task = store.require(video_id)
    if task.status == "submission_unknown":
        raise Yt2BiliError(f"{video_id} 的投稿结果待核对，请先核对创作中心，不能直接重试。")
    if task.status in ("submitted", "submission_unknown"):
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
        if task.status != "submission_unknown":
            task.status = "failed"
        task.error = str(exc)
        store.upsert(task)
        raise
    except Exception as exc:
        if task.status != "submission_unknown":
            task.status = "failed"
        task.error = f"未预期错误：{exc}"
        store.upsert(task)
        raise Yt2BiliError(task.error) from exc
    finally:
        _detach_file_handler(log_path, video_id)
        _current.video_id = None


@_lock_existing_task
def repair(
    settings: Settings, store: TaskStore, video_id: str, *, redownload: bool = False,
    encoder: str = "libx264",
    max_size_gb: float | None = None,
) -> Path:
    """Prepare a replacement file without publishing a duplicate Bilibili post."""
    media.require_ffmpeg()
    task = store.require(video_id)
    work_dir = Path(task.work_dir) if task.work_dir else settings.work_dir / video_id
    work_dir.mkdir(parents=True, exist_ok=True)
    # Resolve metadata first so network errors do not disturb local files.
    meta = youtube.fetch_meta(task.url, settings)
    meta.save(work_dir / "meta.json")
    video_path = work_dir / "video.mp4"
    result = None
    if redownload:
        for path in list(work_dir.glob("source.*")) + list(work_dir.glob("audio.*")) + [video_path]:
            if path.is_file():
                youtube.quarantine_file(path)
    elif _ok(video_path):
        try:
            result = media.prepare_upload_video(video_path, video_path, meta.duration, encoder=encoder, max_size_gb=max_size_gb)
        except InvalidMediaError as exc:
            logger.warning("旧文件不可复用：%s", exc)
            youtube.quarantine_file(video_path)
    if result is None:
        source = youtube.download_video(meta.webpage_url, work_dir, settings, expected_duration=meta.duration)
        result = media.prepare_upload_video(source, video_path, meta.duration, encoder=encoder, max_size_gb=max_size_gb)
    # Preserve the existing submitted status/BV; upload acceptance is not transcode success.
    task.work_dir = str(work_dir)
    task.video_path = str(result)
    store.upsert(task)
    logger.info("已准备并校验替换文件：%s；原稿件 %s，请在创作中心替换视频。", result, task.bv_id)
    return result


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
    task_lock = work_lock(settings.work_dir / meta.video_id)
    task_lock.__enter__()
    _current.video_id = meta.video_id
    log_path: Path | None = None
    try:
        existing = store.get(meta.video_id)
        if existing and existing.status in ("submitted", "submission_unknown") and not force:
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
            if task.status != "submission_unknown":
                task.status = "failed"
            task.error = str(exc)
            store.upsert(task)
            raise
        except Exception as exc:
            if task.status != "submission_unknown":
                task.status = "failed"
            task.error = f"未预期错误：{exc}"
            store.upsert(task)
            raise Yt2BiliError(task.error) from exc
    finally:
        if log_path is not None:
            _detach_file_handler(log_path, meta.video_id)
        _current.video_id = None

        task_lock.__exit__(None, None, None)


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
    ready = False
    if _ok(video_path):
        try:
            video_path = media.prepare_upload_video(video_path, video_path, meta.duration)
            ready = True
        except InvalidMediaError as exc:
            log.warning("旧投稿文件不完整，准备重新下载：%s", exc)
            youtube.quarantine_file(video_path)
    if not ready:
        source = youtube.download_video(
            meta.webpage_url,
            work_dir,
            settings,
            expected_duration=meta.duration,
        )
        video_path = media.prepare_upload_video(source, video_path, meta.duration)
    else:
        log.info("已有 video.mp4 通过校验。")
    task.video_path = str(video_path)
    store.upsert(task)

    _prepare_assets(settings, store, task, meta, work_dir, video_path)
    return _submit_ready(settings, store, task, meta, work_dir, dry_run=dry_run)


def _prepare_assets(settings, store, task, meta, work_dir: Path, video_path: Path) -> None:
    """Prepare submission assets, outside the batch's download/validation queues."""
    log = logging.getLogger(f"yt2bili.task.{task.video_id}")

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


def _submit_ready(settings, store, task, meta, work_dir: Path, *, dry_run: bool) -> Task:
    log = logging.getLogger(f"yt2bili.task.{task.video_id}")
    video_path = Path(task.video_path)
    cover_path = Path(task.cover_path)
    if dry_run:
        task.status = "ready"
        log.info("dry-run：已跳过 B 站上传。")
        store.upsert(task)
        return task

    task.status = "uploading"
    store.upsert(task)
    try:
        bv = _upload_serialized(
            settings, log, video_path, cover_path, task.title_zh, task.desc_zh, meta.webpage_url,
        )
    except Exception:
        task.status = "submission_unknown"
        task.error = "投稿过程未确认完成，请核对创作中心后再操作。"
        store.upsert(task)
        raise
    task.bv_id = bv
    task.status = "submitted" if bv else "submission_unknown"
    task.error = ""
    store.upsert(task)
    if bv:
        _cleanup_uploaded_files(settings, work_dir)
        if not work_dir.exists():
            task.work_dir = ""
            task.video_path = ""
            task.cover_path = ""
            store.upsert(task)
    else:
        log.warning("上传命令退出成功，但未获取到 BV 号，暂不删除本地文件：%s", work_dir)
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
    with _upload_lock, upload_guard(settings):
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

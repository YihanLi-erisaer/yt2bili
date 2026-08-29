from __future__ import annotations

import logging
import shutil
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

    meta = youtube.fetch_meta(url, settings.youtube_cookies)
    existing = store.get(meta.video_id)
    if existing and existing.status == "submitted" and not force:
        raise Yt2BiliError(
            f"{meta.video_id} 已经投稿成功"
            + (f"（{existing.bv_id}）" if existing.bv_id else "")
            + "。若要重做请加 --force。"
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
    _attach_file_handler(work_dir / "pipeline.log")

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

    work_dir = Path(task.work_dir) if task.work_dir else settings.work_dir / video_id
    work_dir.mkdir(parents=True, exist_ok=True)
    _attach_file_handler(work_dir / "pipeline.log")

    meta_path = work_dir / "meta.json"
    if meta_path.is_file():
        meta = YoutubeMeta.load(meta_path)
    else:
        meta = youtube.fetch_meta(task.url, settings.youtube_cookies)

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


def _execute(
    settings: Settings,
    store: TaskStore,
    task: Task,
    meta: YoutubeMeta,
    work_dir: Path,
    *,
    dry_run: bool,
) -> Task:
    meta.save(work_dir / "meta.json")
    task.title_orig = meta.title
    task.desc_orig = meta.description
    task.uploader = meta.uploader
    task.work_dir = str(work_dir)
    task.error = ""

    task.status = "fetching_meta"
    store.upsert(task)
    logger.info("视频：%s | %s", meta.video_id, meta.title)

    video_path = work_dir / "video.mp4"
    task.status = "downloading"
    store.upsert(task)
    if not _ok(video_path):
        source = youtube.download_video(meta.webpage_url, work_dir, settings.youtube_cookies)
        media.ensure_bilibili_mp4(source, video_path)
    else:
        logger.info("已存在 video.mp4，跳过下载/转码。")
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
            logger.warning("封面下载失败，改为从视频抽帧：%s", exc)
            frame = work_dir / "frame.jpg"
            media.extract_frame_cover(video_path, frame)
            media.process_cover(
                frame,
                cover_path,
                settings.cover_width,
                settings.cover_height,
            )
    else:
        logger.info("已存在 cover.jpg，跳过封面处理。")
    task.cover_path = str(cover_path)
    store.upsert(task)

    task.status = "translating"
    store.upsert(task)
    if not task.title_zh:
        footer_reserve = 80 + len(meta.title) + len(meta.uploader) + len(meta.webpage_url)
        body_limit = max(200, settings.desc_limit - footer_reserve)
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
        logger.info("已有中文标题，跳过翻译。")

    (work_dir / "title.txt").write_text(task.title_zh, encoding="utf-8")
    (work_dir / "desc.txt").write_text(task.desc_zh, encoding="utf-8")
    logger.info("中文标题：%s", task.title_zh)
    logger.info("简介预览：\n%s", task.desc_zh)

    if dry_run:
        task.status = "ready"
        logger.info("dry-run：已跳过 B 站上传。")
        store.upsert(task)
        return task

    task.status = "uploading"
    store.upsert(task)
    try:
        bili_upload.renew(settings)
    except Yt2BiliError:
        raise
    except Exception as exc:
        logger.warning("刷新登录态时出错，继续投稿：%s", exc)

    bv = bili_upload.upload(
        settings,
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
    logger.info("完成。投稿成功不等于已过审。")
    return task


def _ok(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _attach_file_handler(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    marker = str(log_path.resolve())
    for handler in root.handlers:
        if getattr(handler, "_yt2bili_path", None) == marker:
            return
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler._yt2bili_path = marker  # type: ignore[attr-defined]
    root.addHandler(handler)

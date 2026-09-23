"""Three persistent, single-lane stage queues for the desktop worker."""
from __future__ import annotations

import logging
import queue
import shutil
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

from yt2bili import events, media, pipeline
from yt2bili.db import Task
from yt2bili.exceptions import InvalidMediaError, Yt2BiliError
from yt2bili.locking import work_lock
from yt2bili.youtube import YoutubeMeta


@dataclass
class ScheduledJob:
    video_id: str
    job: pipeline._PipelineJob
    settings: object
    mode: str = "preview"
    original_status: str = ""
    stage: str = "download"
    cancel: threading.Event = field(default_factory=threading.Event)
    lock: object = None


class Scheduler:
    def __init__(self, store, config, emit):
        self.store, self.config, self.emit = store, config, emit
        self.guard = threading.RLock()
        self.active = {}
        self.closing = False
        self.queues = {name: queue.Queue() for name in ("download", "validate", "upload")}
        for task in store.list_all():
            if task.status in ("uploading", "submission_unknown") or (task.status == "submitted" and not task.bv_id):
                task.status = "submission_unknown"
                task.error = "请核对 B 站创作中心，确认是否已提交；不会自动重复投稿。"
            elif task.status not in ("submitted", "ready", "failed", "cancelled", "interrupted"):
                saved = store.get_job(task.video_id) or {}
                task.status = saved.get("original_status") or "interrupted"
                task.error = "上次运行中断，素材已保留，可手动继续。"
            else:
                continue
            store.upsert(task)
        self.threads = [threading.Thread(target=self._work, args=(stage,), name=f"desktop-{stage}", daemon=True)
                        for stage in self.queues]
        for thread in self.threads:
            thread.start()

    def add(self, task: Task, mode="preview", snapshot=None, stage="download", repair=False):
        with self.guard:
            if self.closing:
                raise Yt2BiliError("应用正在退出，不能添加任务。")
            if task.video_id in self.active:
                raise Yt2BiliError("此任务已在队列中。")
            saved = snapshot or self.config.snapshot()
            settings = self.config.build(saved)
            if task.work_dir:
                settings = replace(settings, work_dir=Path(task.work_dir).parent)
            settings.work_dir.mkdir(parents=True, exist_ok=True)
            work = settings.work_dir / task.video_id
            lock = work_lock(work)
            lock.__enter__()
            original = task.status if repair or mode == "retranslate" else ""
            job = pipeline._PipelineJob(task.url)
            item = ScheduledJob(task.video_id, job, settings, "repair" if repair else mode, original, stage, lock=lock)
            try:
                if stage in ("upload", "validate"):
                    meta_path = Path(task.work_dir) / "meta.json"
                    if not meta_path.is_file():
                        raise Yt2BiliError("缺少素材信息，请先重新准备素材。")
                    job.task, job.meta, job.work_dir = task, YoutubeMeta.load(meta_path), Path(task.work_dir)
                    job.source = Path(task.video_path)
                task.status = {"upload": "queued_upload", "validate": "queued_validation", "download": "queued_download"}[stage]
                task.error = ""
                task.work_dir = str(work)
                self.store.save_job(task.video_id, {"mode": mode, "settings": saved, "original_status": original})
                self.store.upsert(task)
                self.active[task.video_id] = item
                self.queues[stage].put(item)
            except BaseException:
                lock.__exit__(None, None, None)
                raise

    def cancel(self, video_id):
        with self.guard:
            item = self.active.get(video_id)
            if not item:
                raise Yt2BiliError("任务未运行。")
            task = self.store.require(video_id)
            if task.status == "uploading":
                raise Yt2BiliError("正在提交，请等待本次上传结束，避免产生无法确认的投稿结果。")
            item.cancel.set()
            task.status = "cancel_requested"
            self.store.upsert(task)

    def _work(self, stage):
        while True:
            item = self.queues[stage].get()
            if item is None:
                self.queues[stage].task_done()
                return
            finished = False
            try:
                item.stage = stage
                with events.task_context(item.video_id, item.cancel, self.emit):
                    if stage == "download":
                        if shutil.disk_usage(item.settings.work_dir).free < 512 * 1024 * 1024:
                            raise Yt2BiliError("工作磁盘可用空间不足 512 MB，请清理空间后重试。")
                        media.require_ffmpeg()
                        pipeline._download_job(item.settings, self.store, item.job, False, set())
                        events.check_cancelled()
                        if item.job.skipped:
                            finished = True
                        else:
                            self.queues["validate"].put(item)
                    elif stage == "validate":
                        pipeline._validate_job(item.settings, self.store, item.job)
                        events.check_cancelled()
                        if item.mode == "repair":
                            item.job.task.status = item.original_status
                            self.store.upsert(item.job.task)
                            self.emit("task.repaired", {"video_id": item.video_id, "path": item.job.task.video_path})
                            finished = True
                        else:
                            self.queues["upload"].put(item)
                    else:
                        # Explicit submission revalidates the edited ready task without retranslation.
                        if item.mode == "retranslate":
                            from yt2bili.translation.tasks import prepare
                            item.job.task.status = "translating"
                            self.store.upsert(item.job.task)
                            prepare(item.settings, self.store, item.job.task, item.job.meta, item.job.work_dir, force=True)
                            item.job.task.status = "ready"
                            self.store.upsert(item.job.task)
                        elif item.mode == "submit":
                            with pipeline._job_context(item.job):
                                events.check_cancelled()
                                pipeline._submit_ready(item.settings, self.store, item.job.task, item.job.meta, item.job.work_dir, dry_run=False)
                        else:
                            pipeline._upload_job(item.settings, self.store, item.job, item.mode == "preview")
                        finished = True
            except Exception as exc:
                current = self.store.require(item.video_id)
                if stage == "validate" and item.mode != "submit" and isinstance(exc, InvalidMediaError) and item.job.attempts < pipeline.MAX_MEDIA_ATTEMPTS and not item.cancel.is_set():
                    try:
                        pipeline._reject_job_source(item.job)
                        current.status = "queued_download"
                        current.error = f"校验未通过，重新下载（{item.job.attempts}/{pipeline.MAX_MEDIA_ATTEMPTS}）"
                        self.store.upsert(current)
                        self.queues["download"].put(item)
                        continue
                    except Exception as recovery:
                        exc = recovery
                current.status = (item.original_status if item.mode in ("repair", "retranslate") else
                                  "submission_unknown" if current.status in ("uploading", "submission_unknown") else
                                  "cancelled" if item.cancel.is_set() else "failed")
                current.error = str(exc)
                self.store.upsert(current)
                logging.getLogger(__name__).warning("[%s] %s", item.video_id, exc)
                finished = True
            finally:
                if finished:
                    with self.guard:
                        self.active.pop(item.video_id, None)
                        item.lock.__exit__(None, None, None)
                    self.emit("queue.changed", self.snapshot())
                self.queues[stage].task_done()

    def snapshot(self):
        with self.guard:
            return {"active": [{"video_id": item.video_id, "stage": item.stage, "mode": item.mode} for item in self.active.values()]}

    def close(self):
        with self.guard:
            self.closing = True
            for item in self.active.values():
                if self.store.require(item.video_id).status != "uploading":
                    item.cancel.set()
        # Drain before inserting sentinels: stages can enqueue into one another.
        for queue_ in self.queues.values():
            queue_.join()
        for queue_ in self.queues.values():
            queue_.put(None)
        for thread in self.threads:
            thread.join()

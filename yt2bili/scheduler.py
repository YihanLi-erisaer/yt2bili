"""Shared download/validation workers and one serial lane per bound account."""
from __future__ import annotations

import logging
import queue
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

from yt2bili import events, media, pipeline
from yt2bili.db import Task
from yt2bili.exceptions import InvalidMediaError, Yt2BiliError
from yt2bili.locking import work_lock
from yt2bili.youtube import YoutubeMeta
from yt2bili.upload_coordinator import UploadCoordinator
from yt2bili.task_paths import validate_task_paths


@dataclass
class ScheduledJob:
    task_id: str
    run_id: str
    job: pipeline._PipelineJob
    settings: object
    payload: dict
    stage: str = "download"
    cancel: threading.Event = field(default_factory=threading.Event)
    lock: object = None
    assets_complete: bool = False
    deadline: float | None = None
    wait_reason: str = ""
    sequence: int = 0
    running: bool = False
    running_action: str = ""

    @property
    def mode(self): return self.payload["mode"]

    @property
    def video_id(self): return self.job.task.video_id


class AccountLane:
    def __init__(self, scheduler, account_id):
        self.scheduler, self.account_id = scheduler, account_id
        self.condition = threading.Condition()
        self.items = []
        self.stopped = False
        self.thread = threading.Thread(target=self.work, name=f"upload-{account_id[:8]}", daemon=True)
        self.thread.start()

    def put(self, item):
        with self.condition:
            self.items.append(item)
            self.items.sort(key=lambda x: x.sequence)
            self.condition.notify_all()

    def wake(self, credentials=False):
        with self.condition:
            if credentials:
                for item in self.items:
                    item.deadline, item.wait_reason = 0, ""
            self.condition.notify_all()

    def work(self):
        while True:
            with self.condition:
                if self.stopped and not self.items:
                    return
                items = list(self.items)
            item = next((i for i in items if i.cancel.is_set()), None)
            action = "cancel"
            if item is None:
                head = next((i for i in items if i.mode != "preview"), None)
                if head and head.assets_complete and (not head.wait_reason or
                        head.deadline is not None and time.monotonic() >= head.deadline):
                    item, action = head, "submit"
                else:
                    item = next((i for i in items if not i.assets_complete), None)
                    action = "prepare"
            if item is None:
                with self.condition:
                    self.condition.wait(.25)
                continue
            finished = False
            try:
                item.running = True
                item.running_action = action
                with self.scheduler.context(item):
                    events.check_cancelled()
                    if action == "prepare":
                        task = self.scheduler.store.require(item.task_id)
                        item.job.task = task
                        with pipeline._job_context(item.job):
                            pipeline._prepare_assets(item.settings, self.scheduler.store, task,
                                item.job.meta, item.job.work_dir, Path(task.video_path))
                        events.check_cancelled()
                        item.assets_complete = True
                        if item.mode == "preview":
                            self.scheduler.store.update(item.task_id, status="ready", wait_reason="")
                            finished = True
                        else:
                            self.scheduler.store.update(item.task_id, status="queued_upload", wait_reason="queue")
                    else:
                        reason, deadline = self.scheduler.uploader.try_submit(item)
                        finished = reason == "finished"
                        if not finished:
                            item.wait_reason, item.deadline = reason, deadline
                            current = self.scheduler.store.require(item.task_id)
                            if current.wait_reason != reason:
                                self.scheduler.store.update(item.task_id, status="queued_upload", wait_reason=reason)
                    self.scheduler.persist(item)
            except Exception as exc:
                finished = True
                self.scheduler.fail(item, exc)
            finally:
                item.running = False
                item.running_action = ""
                if finished:
                    with self.condition:
                        self.items.remove(item)
                    self.scheduler.finish(item)


class Scheduler:
    def __init__(self, store, config, emit, accounts):
        self.store, self.config, self.emit, self.accounts = store, config, emit, accounts
        self.guard = threading.RLock()
        self.active, self.upload_lanes = {}, {}
        self.closing = False
        self.closed = threading.Event()
        self.signal = threading.Event()
        self.session_id = str(uuid.uuid4())
        self.queue_revision = 0
        self.queues = {name: queue.Queue() for name in ("download", "validate")}
        self.uploader = UploadCoordinator(store, accounts)
        self._recover()
        self.sync_accounts()
        self.threads = [threading.Thread(target=self._work, args=(stage,), name=f"desktop-{stage}", daemon=True)
                        for stage in self.queues]
        for thread in self.threads:
            thread.start()
        self.dispatcher = threading.Thread(target=self._dispatch, name="desktop-dispatch", daemon=True)
        self.dispatcher.start()

    def _recover(self):
        for task in self.store.list_all():
            saved = self.store.get_job(task.task_id) or {}
            if task.status in ("uploading", "submission_unknown") or task.status == "submitted" and not task.bv_id:
                self.store.update(task.task_id, status="submission_unknown", error="请核对该账号创作中心；不会自动重复投稿。")
            elif task.cancel_requested and task.status not in ("submitted", "failed", "cancelled"):
                self.store.update(task.task_id, status="cancelled", error="上次取消已保留素材，请手动继续。")
            elif task.status not in ("submitted", "ready", "failed", "cancelled", "interrupted"):
                self.store.update(task.task_id, status=saved.get("original_status") or "interrupted",
                                  error="上次运行中断，素材保留，请手动继续。")
            if saved:
                saved["execution_state"] = "interrupted"
                self.store.save_job(task.task_id, saved)

    def sync_accounts(self, changed_account=None):
        with self.guard:
            wanted = {a["account_id"] for a in self.store.accounts()}
            for account_id in wanted:
                if account_id not in self.upload_lanes:
                    self.upload_lanes[account_id] = AccountLane(self, account_id)
            for account_id in list(self.upload_lanes):
                if account_id not in wanted:
                    lane = self.upload_lanes.pop(account_id)
                    with lane.condition:
                        lane.stopped = True
                        lane.condition.notify_all()
            for key, lane in self.upload_lanes.items():
                lane.wake(credentials=key == changed_account)

    def context(self, item):
        return events.task_context(item.video_id, item.cancel, self.emit, task_id=item.task_id,
                                   account_id=item.job.task.account_id, run_id=item.run_id)

    def add(self, task: Task, mode="preview", snapshot=None, stage="download", repair=False):
        if self.closing:
            raise Yt2BiliError("应用正在退出，不能添加任务。")
        self.store.account(task.account_id, active=not repair)
        with self.store.transaction():
            saved = self.store.get_job(task.task_id) or {}
            if saved.get("owner_session_id") == self.session_id and saved.get("execution_state") in ("queued", "running", "waiting"):
                raise Yt2BiliError("此任务已在队列中。")
            values = snapshot or self.config.snapshot()
            if not task.work_root:
                task.work_root = values["work_dir"]
            if not task.work_dir:
                task.work_dir = str(Path(task.work_root) / task.task_id)
            validate_task_paths(task)
            original = task.status if repair else ""
            task.status = {"download": "queued_download", "validate": "queued_validation", "upload": "queued_upload"}[stage]
            task.error, task.wait_reason, task.cancel_requested = "", "", 0
            self.store._conn.execute("UPDATE tasks SET cancel_requested=0 WHERE task_id=?", (task.task_id,))
            self.store.upsert(task)
            self.store.save_job(task.task_id, dict(mode="repair" if repair else mode, settings=values,
                original_status=original, stage=stage, execution_state="queued",
                run_id=str(uuid.uuid4()), owner_session_id=self.session_id, sequence=self.store.next_sequence(),
                media_attempts=0, assets_complete=mode == "submit"))
        self.signal.set()

    def _dispatch(self):
        while not self.closed.is_set():
            self.signal.wait(.25)
            self.signal.clear()
            pending = [(task, self.store.get_job(task.task_id) or {}) for task in self.store.list_all()]
            pending = [(task, saved) for task, saved in pending if saved.get("owner_session_id") == self.session_id and saved.get("execution_state") == "queued"]
            for task, saved in sorted(pending, key=lambda pair: pair[1]["sequence"]):
                with self.guard:
                    if task.task_id in self.active:
                        continue
                    # A cancel/retry may have committed since the scan above.
                    current = self.store.get_job(task.task_id) or {}
                    if current.get("run_id") != saved.get("run_id") or current.get("execution_state") != "queued":
                        continue
                    task = self.store.require(task.task_id)
                    if self.closing or task.cancel_requested:
                        self.store.update(task.task_id, status="cancelled", wait_reason="")
                        saved["execution_state"] = "finished"
                        self.store.save_job(task.task_id, saved)
                        continue
                    lock = work_lock(Path(task.work_dir))
                    try:
                        lock.__enter__()
                        settings = replace(self.config.build(saved["settings"]), work_dir=Path(task.work_root))
                        Path(task.work_dir).mkdir(parents=True, exist_ok=True)
                        job = pipeline._PipelineJob(task.url, task=task, work_dir=Path(task.work_dir))
                        job.attempts = saved.get("media_attempts", 0)
                        if saved["stage"] != "download":
                            job.meta = YoutubeMeta.load(Path(task.work_dir) / "meta.json")
                            job.source = Path(task.video_path)
                        item = ScheduledJob(task.task_id, saved["run_id"], job, settings, saved,
                                            saved["stage"], lock=lock, assets_complete=saved.get("assets_complete", False),
                                            sequence=saved["sequence"])
                        self.active[task.task_id] = item
                        self.persist(item)
                        self._put(item)
                    except Exception as exc:
                        self.active.pop(task.task_id, None)
                        lock.__exit__(None, None, None)
                        saved["execution_state"] = "finished"
                        self.store.save_job(task.task_id, saved)
                        self.store.update(task.task_id, status="failed", error=str(exc))
            self.emit("queue.changed", self.snapshot())

    def _put(self, item):
        if item.stage == "upload":
            self.upload_lanes[item.job.task.account_id].put(item)
        else:
            self.queues[item.stage].put(item)

    def persist(self, item, finished=False):
        item.payload.update(stage=item.stage, execution_state="finished" if finished else "running",
                            media_attempts=item.job.attempts, assets_complete=item.assets_complete, sequence=item.sequence)
        self.store.save_job(item.task_id, item.payload)

    def route(self, item, stage):
        events.check_cancelled()
        item.stage, item.sequence = stage, self.store.next_sequence()
        self.store.update(item.task_id, status={"download":"queued_download","validate":"queued_validation","upload":"queued_upload"}[stage])
        self.persist(item)
        self._put(item)

    def cancel(self, identity):
        with self.store.transaction():
            task = self.store.require(identity)
            if task.status == "uploading":
                raise Yt2BiliError("正在提交，请等待上传结束并核对结果。")
            saved = self.store.get_job(task.task_id) or {}
            if task.status in ("submitted", "submission_unknown"):
                raise Yt2BiliError("该任务不能取消，请先核对投稿结果。")
            self.store.update(task.task_id, cancel_requested=1, status="cancel_requested")
        with self.guard:
            item = self.active.get(task.task_id)
            if item:
                item.cancel.set()
            else:
                self.store.update(task.task_id, status="cancelled", wait_reason="")
                saved["execution_state"] = "finished"
                self.store.save_job(task.task_id, saved)
            for lane in self.upload_lanes.values():
                lane.wake()
        self.signal.set()

    def _work(self, stage):
        while True:
            item = self.queues[stage].get()
            if item is None:
                self.queues[stage].task_done()
                return
            finished = False
            try:
                item.running = True
                with self.context(item):
                    if stage == "download":
                        if shutil.disk_usage(item.settings.work_dir).free < 512 * 1024 * 1024:
                            raise Yt2BiliError("工作磁盘可用空间不足 512 MB。")
                        media.require_ffmpeg()
                        pipeline._download_job(item.settings, self.store, item.job, False, set())
                        self.route(item, "validate")
                    else:
                        pipeline._validate_job(item.settings, self.store, item.job)
                        events.check_cancelled()
                        if item.mode == "repair":
                            self.store.update(item.task_id, status=item.payload["original_status"])
                            self.emit("task.repaired", {"task_id": item.task_id, "video_id": item.video_id,
                                                      "account_id": item.job.task.account_id, "path": item.job.task.video_path})
                            finished = True
                        else:
                            self.route(item, "upload")
            except Exception as exc:
                if stage == "validate" and item.mode != "submit" and isinstance(exc, InvalidMediaError) and item.job.attempts < pipeline.MAX_MEDIA_ATTEMPTS and not item.cancel.is_set():
                    try:
                        pipeline._reject_job_source(item.job)
                        with self.context(item):
                            self.route(item, "download")
                    except Exception as recovery:
                        self.fail(item, recovery)
                        finished = True
                else:
                    finished = True
                    self.fail(item, exc)
            finally:
                item.running = False
                if finished:
                    self.finish(item)
                self.queues[stage].task_done()

    def fail(self, item, exc):
        current = self.store.require(item.task_id)
        status = item.payload["original_status"] if item.mode == "repair" else (
            current.status if current.status in ("submitted", "submission_unknown") else
            "submission_unknown" if current.status == "uploading" else
            "cancelled" if item.cancel.is_set() else "failed")
        self.store.update(item.task_id, status=status, error=str(exc), wait_reason="")
        with events.task_context(item.video_id, None, self.emit, task_id=item.task_id,
                                 account_id=item.job.task.account_id, run_id=item.run_id):
            logging.getLogger(__name__).warning("[%s] %s", item.task_id, exc)

    def finish(self, item):
        self.persist(item, finished=True)
        with self.guard:
            current = self.store.require(item.task_id)
            if current.cancel_requested and current.status not in ("submitted", "submission_unknown", "uploading"):
                self.store.update(item.task_id, status="cancelled", wait_reason="")
            self.active.pop(item.task_id, None)
            item.lock.__exit__(None, None, None)
        self.emit("queue.changed", self.snapshot())

    def snapshot(self):
        with self.guard:
            self.queue_revision += 1
            items = list(self.active.values())
            active = [{"task_id": i.task_id, "video_id": i.video_id, "account_id": i.job.task.account_id,
                       "stage": i.stage, "mode": i.mode, "run_id": i.run_id} for i in items]
            for task in self.store.list_all():
                saved = self.store.get_job(task.task_id) or {}
                if task.task_id not in self.active and saved.get("owner_session_id") == self.session_id and saved.get("execution_state") == "queued":
                    active.append({"task_id": task.task_id, "video_id": task.video_id, "account_id": task.account_id,
                                   "stage": saved["stage"], "mode": saved["mode"], "run_id": saved["run_id"]})
            def shared(stage):
                selected = [i for i in items if i.stage == stage]
                return {"running_task_id": next((i.task_id for i in selected if i.running), None),
                        "queued_count": sum(i["stage"] == stage and not any(j.task_id == i["task_id"] and j.running for j in selected) for i in active)}
            uploads = []
            for account_id, lane in self.upload_lanes.items():
                selected = [i for i in items if i.stage == "upload" and i.job.task.account_id == account_id]
                current = next((i for i in selected if i.running), None)
                waiting = next((i for i in selected if i.wait_reason), None)
                uploads.append({"account_id": account_id, "running_task_id": current.task_id if current else None,
                    "running_action": current.running_action if current else None,
                    "queued_count": sum(not i.running for i in selected), "wait_reason": waiting.wait_reason if waiting else "",
                    "remaining_seconds": max(0, waiting.deadline-time.monotonic()) if waiting and waiting.deadline else None})
            return {"queue_revision": self.queue_revision, "active": active, "download": shared("download"), "validate": shared("validate"), "uploads": uploads}

    def prepare_shutdown(self):
        self.closing = True
        for task in self.store.list_all():
            saved = self.store.get_job(task.task_id) or {}
            if saved.get("owner_session_id") == self.session_id and saved.get("execution_state") in ("running", "queued", "waiting") and task.status != "uploading":
                try:
                    self.cancel(task.task_id)
                except Yt2BiliError:
                    pass
        self.signal.set()
        return self.shutdown_status()

    def shutdown_status(self):
        pending = [t.task_id for t in self.store.list_all()
                   if (self.store.get_job(t.task_id) or {}).get("owner_session_id") == self.session_id
                   and (self.store.get_job(t.task_id) or {}).get("execution_state") in ("queued", "running", "waiting")]
        return {"closing": self.closing, "ready": self.closing and not pending and not self.active,
                "pending_task_ids": pending}

    def close(self):
        self.prepare_shutdown()
        while not self.shutdown_status()["ready"]:
            self.signal.set()
            time.sleep(.05)
        self.closed.set()
        self.signal.set()
        self.dispatcher.join()
        for queue_ in self.queues.values():
            queue_.put(None)
        for thread in self.threads:
            thread.join()
        for lane in self.upload_lanes.values():
            with lane.condition:
                lane.stopped = True
                lane.condition.notify_all()
            lane.thread.join()

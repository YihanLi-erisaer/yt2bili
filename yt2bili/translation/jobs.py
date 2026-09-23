"""Persisted background deployment/test jobs; never block the desktop RPC pool."""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from yt2bili import events
from yt2bili.desktop_settings import atomic_json
from yt2bili.exceptions import Yt2BiliError


class TranslationJobs:
    def __init__(self, root, emit):
        self.path = Path(root) / "jobs.json"
        self.emit, self.lock = emit, threading.RLock()
        self.records, self.running = {}, {}
        self.closing = False
        if self.path.is_file():
            try:
                self.records = json.loads(self.path.read_text(encoding="utf-8"))
                for record in self.records.values():
                    if record["state"] in ("running", "cancel_requested"):
                        record.update(state="interrupted", message="上次操作中断，可重试。")
            except (ValueError, KeyError, TypeError):
                self.records = {}

    def persist(self):
        atomic_json(self.path, self.records)

    def active(self):
        with self.lock:
            return bool(self.running)

    def start(self, kind, operation_id, action):
        if not isinstance(operation_id, str) or not 8 <= len(operation_id) <= 100:
            raise Yt2BiliError("缺少有效操作 ID。")
        with self.lock:
            if self.closing:
                raise Yt2BiliError("应用正在退出。")
            for value in self.records.values():
                if value["operation_id"] == operation_id:
                    if value["kind"] != kind:
                        raise Yt2BiliError("操作 ID 已用于其他请求。")
                    return {"job_id": value["job_id"]}
            if self.running:
                raise Yt2BiliError("已有翻译组件操作运行，请等待或取消。")
            job_id = str(uuid.uuid4())
            self.records = dict(list(self.records.items())[-49:])
            self.records[job_id] = {"job_id": job_id, "operation_id": operation_id, "kind": kind, "state": "running"}
            cancel = threading.Event()
            def progress(event, payload):
                with self.lock:
                    self.records[job_id]["progress"] = payload
                self.emit("translation.job.progress", {"job_id": job_id, **payload})
            def run():
                try:
                    with events.task_context(job_id, cancel, progress):
                        result = action()
                        events.check_cancelled()
                    update = {"state": "complete", "result": result}
                except events.Cancelled:
                    update = {"state": "cancelled", "message": "操作已取消；可重试复用已下载资源。"}
                except Exception as exc:
                    update = {"state": "failed", "message": str(exc) if isinstance(exc, Yt2BiliError) else "操作未完成，请检查网络、磁盘空间和文件权限。"}
                with self.lock:
                    self.records[job_id].update(update)
                    self.running.pop(job_id, None)
                    self.persist()
                self.emit("translation.job.finished", self.get(job_id))
            thread = threading.Thread(target=run, name="translation-job", daemon=True)
            self.running[job_id] = (cancel, thread)
            self.persist()
            thread.start()
            return {"job_id": job_id}

    def get(self, job_id=None):
        with self.lock:
            if job_id is None:
                return {"items": [dict(v) for v in self.records.values()]}
            if job_id not in self.records:
                raise Yt2BiliError("找不到组件操作。")
            return dict(self.records[job_id])

    def cancel(self, job_id):
        with self.lock:
            self.get(job_id)
            if job_id in self.running:
                self.running[job_id][0].set()
                self.records[job_id]["state"] = "cancel_requested"
                self.persist()
            return {"requested": True}

    def close(self):
        with self.lock:
            self.closing = True
            running = list(self.running.values())
            for cancel, _ in running:
                cancel.set()
        for _, thread in running:
            thread.join()

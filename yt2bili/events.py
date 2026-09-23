"""Thread-scoped cancellation and progress, shared by CLI and desktop stages."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from yt2bili.exceptions import Yt2BiliError


class Cancelled(Yt2BiliError):
    pass


_local = threading.local()


@contextmanager
def task_context(video_id, cancel, emit, *, task_id=None, account_id=None, run_id=None):
    previous = getattr(_local, "context", None)
    _local.context = (video_id, cancel, emit)
    previous_identity = getattr(_local, "identity", {})
    _local.identity = {"task_id": task_id or video_id, "account_id": account_id, "run_id": run_id}
    _local.last_progress = 0.0
    try:
        check_cancelled()
        yield
    finally:
        _local.context = previous
        _local.identity = previous_identity


def current_identity():
    return dict(getattr(_local, "identity", {}))


def check_cancelled():
    context = getattr(_local, "context", None)
    if context and context[1] is not None and context[1].is_set():
        raise Cancelled("任务已取消，已保留可恢复的素材。")


def current_video_id():
    context = getattr(_local, "context", None)
    return context[0] if context else None


def cancellation_event():
    context = getattr(_local, "context", None)
    return context[1] if context else None


def progress(stage, *, force=False, **payload):
    check_cancelled()
    context = getattr(_local, "context", None)
    if not context:
        return
    now = time.monotonic()
    if not force and now - getattr(_local, "last_progress", 0) < 0.25:
        return
    _local.last_progress = now
    context[2]("task.progress", {"video_id": context[0], **current_identity(), "stage": stage, **payload})


def download_progress(data):
    total = data.get("total_bytes") or data.get("total_bytes_estimate")
    done = data.get("downloaded_bytes", 0)
    progress("downloading", percent=min(100, done / total * 100) if total else None,
             bytes=done, total=total, speed=data.get("speed"), eta=data.get("eta"))

"""OS-backed locks survive crashes without leaving a stale lock owner."""
from __future__ import annotations

import os
import json
import hashlib
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from yt2bili.exceptions import Yt2BiliError


def work_lock(work_dir: Path):
    work_dir = work_dir.resolve()
    name = hashlib.sha256(os.path.normcase(str(work_dir)).encode()).hexdigest()
    return FileLock(work_dir.parent / ".locks" / f"{name}.lock")


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "a+b")
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.file.close()
            self.file = None
            raise Yt2BiliError("另一个进程正在使用该任务或账号，请等待其结束。") from exc
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


@contextmanager
def upload_guard(settings):
    """Conservatively serialize all uploads for this OS user, even copied cookies."""
    from yt2bili import events
    # Preserve library callers which provide minimal test settings without credentials.
    if not hasattr(settings, "bili_cookies"):
        yield
        return
    folder = Path(tempfile.gettempdir()) / "stardazz-yt2bili-upload"
    with FileLock(folder / "account.lock"):
        marker = folder / "last-upload.json"
        try:
            ended = float(json.loads(marker.read_text())["ended"])
        except (OSError, ValueError, KeyError):
            ended = 0
        deadline = time.monotonic() + min(settings.upload_gap_seconds, max(0, ended + settings.upload_gap_seconds - time.time()))
        remaining = max(0, deadline - time.monotonic())
        while remaining > 0:
            events.progress("upload_wait", force=True, remaining=round(remaining, 1), percent=None)
            time.sleep(min(0.25, remaining))
            remaining = max(0, deadline - time.monotonic())
        try:
            yield
        finally:
            from yt2bili.desktop_settings import atomic_json
            atomic_json(marker, {"ended": time.time()})

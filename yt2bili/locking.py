"""OS-backed locks survive crashes without leaving a stale lock owner."""
from __future__ import annotations

import os
import json
import hashlib
import tempfile
import time
import threading
from contextlib import contextmanager
from pathlib import Path

from yt2bili.exceptions import Yt2BiliError

_uid_mutex_guard = threading.Lock()
_uid_mutexes = {}


class AccountBusy(Yt2BiliError):
    pass


def coordination_dir(uid):
    from yt2bili.identity import normalize_uid
    from yt2bili.paths import AppPaths
    override = os.environ.get("YT2BILI_COORDINATION_DIR")
    root = Path(override) if override else AppPaths.default().root / "coordination/bilibili"
    return root / normalize_uid(uid)


@contextmanager
def account_guard(uid):
    """Nonblocking, UID-scoped credential/upload lock shared across profiles."""
    from yt2bili.identity import normalize_uid
    uid = normalize_uid(uid)
    with _uid_mutex_guard:
        mutex = _uid_mutexes.setdefault(uid, threading.Lock())
    if not mutex.acquire(blocking=False):
        raise AccountBusy("账号正在被另一操作使用，请稍后重试。")
    lock = FileLock(coordination_dir(uid) / "upload.lock")
    try:
        try:
            lock.__enter__()
        except Yt2BiliError as exc:
            raise AccountBusy("账号正在被另一进程使用，请稍后重试。") from exc
        try:
            yield
        finally:
            lock.__exit__(None, None, None)
    finally:
        mutex.release()


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

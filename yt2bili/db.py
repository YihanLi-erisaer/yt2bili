from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from yt2bili.exceptions import Yt2BiliError

STATUSES = (
    "pending",
    "fetching_meta",
    "downloading",
    "queued_download",
    "queued_validation",
    "validating",
    "queued_upload",
    "processing_cover",
    "translating",
    "ready",
    "uploading",
    "submitted",
    "failed",
)


@dataclass
class Task:
    video_id: str
    url: str
    status: str
    title_orig: str = ""
    title_zh: str = ""
    desc_orig: str = ""
    desc_zh: str = ""
    uploader: str = ""
    work_dir: str = ""
    video_path: str = ""
    cover_path: str = ""
    bv_id: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


class TaskStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                video_id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                status TEXT NOT NULL,
                title_orig TEXT DEFAULT '',
                title_zh TEXT DEFAULT '',
                desc_orig TEXT DEFAULT '',
                desc_zh TEXT DEFAULT '',
                uploader TEXT DEFAULT '',
                work_dir TEXT DEFAULT '',
                video_path TEXT DEFAULT '',
                cover_path TEXT DEFAULT '',
                bv_id TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def get(self, video_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE video_id = ?", (video_id,)
            ).fetchone()
            return self._row_to_task(row) if row else None

    def list_all(self) -> list[Task]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC"
            ).fetchall()
            return [self._row_to_task(r) for r in rows]

    def upsert(self, task: Task) -> None:
        with self._lock:
            now = _now()
            existing = self.get(task.video_id)
            if existing is None:
                task.created_at = now
            else:
                task.created_at = existing.created_at or now
            task.updated_at = now
            self._conn.execute(
                """
                INSERT INTO tasks (
                    video_id, url, status, title_orig, title_zh, desc_orig, desc_zh,
                    uploader, work_dir, video_path, cover_path, bv_id, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    url=excluded.url,
                    status=excluded.status,
                    title_orig=excluded.title_orig,
                    title_zh=excluded.title_zh,
                    desc_orig=excluded.desc_orig,
                    desc_zh=excluded.desc_zh,
                    uploader=excluded.uploader,
                    work_dir=excluded.work_dir,
                    video_path=excluded.video_path,
                    cover_path=excluded.cover_path,
                    bv_id=excluded.bv_id,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    task.video_id,
                    task.url,
                    task.status,
                    task.title_orig,
                    task.title_zh,
                    task.desc_orig,
                    task.desc_zh,
                    task.uploader,
                    task.work_dir,
                    task.video_path,
                    task.cover_path,
                    task.bv_id,
                    task.error,
                    task.created_at,
                    task.updated_at,
                ),
            )
            self._conn.commit()

    def require(self, video_id: str) -> Task:
        task = self.get(video_id)
        if task is None:
            raise Yt2BiliError(f"找不到任务 {video_id}，请先用 run 跑过该链接。")
        return task

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(**{k: row[k] for k in row.keys()})

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

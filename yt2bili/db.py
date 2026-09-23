from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict, dataclass
import json
from contextlib import closing
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
    "cancel_requested",
    "cancelled",
    "interrupted",
    "submission_unknown",
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
    def __init__(self, db_path: Path, on_change=None) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.on_change = on_change
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            self._conn.close()
            raise Yt2BiliError("数据库版本高于当前程序，请使用更新版本。")
        if version < 2 and db_path.stat().st_size:
            with closing(sqlite3.connect(str(db_path) + (".pre-desktop.bak" if version == 0 else ".pre-translation.bak"))) as backup:
                self._conn.backup(backup)
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
        with self._conn:
            self._conn.execute("CREATE TABLE IF NOT EXISTS desktop_jobs (video_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS desktop_operations (id TEXT PRIMARY KEY, method TEXT NOT NULL, result TEXT NOT NULL)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS task_translation (video_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS translation_attempts (id INTEGER PRIMARY KEY, video_id TEXT NOT NULL, payload TEXT NOT NULL)")
            if version < 2:
                from yt2bili.translation.config import legacy_snapshot
                for row in self._conn.execute("SELECT video_id,payload FROM desktop_jobs").fetchall():
                    job = json.loads(row["payload"])
                    job["settings"] = legacy_snapshot(job.get("settings", {}))
                    self._conn.execute("UPDATE desktop_jobs SET payload=? WHERE video_id=?", (json.dumps(job), row["video_id"]))
                for row in self._conn.execute("SELECT video_id,title_zh,desc_zh FROM tasks").fetchall():
                    record = {"config_snapshot": legacy_snapshot({})}
                    if row["title_zh"]:
                        record.update(state="legacy_preserved", provider="unknown", user_edited=False)
                    self._conn.execute("INSERT OR IGNORE INTO task_translation VALUES (?,?)", (row["video_id"], json.dumps(record)))
            self._conn.execute("PRAGMA user_version=2")

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

    def upsert(self, task: Task, *, commit=True) -> None:
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
            if commit:
                self._conn.commit()
        if commit and self.on_change:
            self.on_change("task.status", asdict(task))

    def translation(self, video_id):
        with self._lock:
            row = self._conn.execute("SELECT payload FROM task_translation WHERE video_id=?", (video_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def reset_translation(self, video_id):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM task_translation WHERE video_id=?", (video_id,))

    def save_translation(self, task, record):
        # The task pair and provenance commit together. upsert must not commit here.
        with self._lock, self._conn:
            self.upsert(task, commit=False)
            self._conn.execute("INSERT OR REPLACE INTO task_translation VALUES (?,?)", (task.video_id, json.dumps(record, ensure_ascii=False)))
        if self.on_change:
            self.on_change("task.status", asdict(task))

    def translation_attempt(self, video_id, attempts):
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO translation_attempts(video_id,payload) VALUES (?,?)", (video_id, json.dumps({"time": _now(), "attempts": attempts})))
            self._conn.execute("DELETE FROM translation_attempts WHERE video_id=? AND id NOT IN (SELECT id FROM translation_attempts WHERE video_id=? ORDER BY id DESC LIMIT 20)", (video_id, video_id))

    def save_job(self, video_id: str, payload: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO desktop_jobs VALUES (?, ?)",
                               (video_id, json.dumps(payload, ensure_ascii=False)))

    def get_job(self, video_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM desktop_jobs WHERE video_id=?", (video_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def operation(self, operation_id: str, method: str, result=None):
        with self._lock, self._conn:
            if result is not None:
                self._conn.execute("INSERT INTO desktop_operations VALUES (?, ?, ?)",
                                   (operation_id, method, json.dumps(result)))
                return result
            row = self._conn.execute("SELECT method, result FROM desktop_operations WHERE id=?", (operation_id,)).fetchone()
            if row and row[0] != method:
                raise Yt2BiliError("操作 ID 已用于另一种请求。")
            return json.loads(row[1]) if row else None

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

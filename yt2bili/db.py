from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from yt2bili.exceptions import Yt2BiliError

SCHEMA_VERSION = 4
STATUSES = ("pending", "fetching_meta", "downloading", "queued_download", "queued_validation",
            "validating", "queued_upload", "processing_cover", "translating", "ready", "uploading",
            "submitted", "failed", "cancel_requested", "cancelled", "interrupted", "submission_unknown",
            "partial_success", "completed_with_abandon")


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
    task_id: str = ""
    account_id: str | None = None
    account_uid_snapshot: str | None = None
    account_name_snapshot: str = ""
    work_root: str = ""
    revision: int = 0
    metadata_revision: int = 0
    wait_reason: str = ""
    cleanup_state: str = "none"
    cancel_requested: int = 0


class TaskStore:
    """Short composable transactions; notifications are emitted after commit."""

    def __init__(self, db_path: Path, on_change=None):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.on_change = db_path, on_change
        self._lock = threading.RLock()
        self._depth, self._events = 0, []
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self._conn.close()
            raise Yt2BiliError("数据库版本高于当前程序，请使用更新版本。")
        if version == SCHEMA_VERSION:
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)")}
            if not {"task_id", "account_id", "account_uid_snapshot"} <= columns:
                self._conn.close()
                raise Yt2BiliError("检测到其他分支的同版本数据库，请先进行联合迁移；原库未修改。")
        if version < SCHEMA_VERSION:
            if db_path.stat().st_size:
                suffix = ".pre-desktop.bak" if version == 0 else (".pre-v4.bak" if version == 3 else ".pre-v3.bak")
                backup_path = Path(str(db_path) + suffix)
                if backup_path.exists():
                    backup_path = Path(str(backup_path) + "." + uuid.uuid4().hex)
                with closing(sqlite3.connect(backup_path)) as backup:
                    self._conn.backup(backup)
            try:
                self._migrate()
                from yt2bili.publications import migrate
                migrate(self)
            except BaseException:
                self._conn.close()
                raise

    def _migrate(self):
        with self.transaction():
            tables = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)")}
            if "task_id" in columns:
                # Early multi-account v2 databases predate these bookkeeping tables.
                # Keep the existing task/account identities and create only missing tables.
                self._conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)")
                self._conn.execute("CREATE TABLE IF NOT EXISTS import_conflicts(source_id TEXT,legacy_id TEXT,payload TEXT,existing_task_id TEXT,PRIMARY KEY(source_id,legacy_id))")
                self._translation_schema()
                if self._conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise Yt2BiliError("数据库迁移引用检查失败。")
                self._conn.execute("PRAGMA user_version=3")
                self._conn.execute("INSERT OR REPLACE INTO schema_migrations VALUES(?,?)", (3, _now()))
                return
            old = [dict(r) for r in self._conn.execute("SELECT * FROM tasks")] if "tasks" in tables else []
            jobs = [dict(r) for r in self._conn.execute("SELECT * FROM desktop_jobs")] if "desktop_jobs" in tables else []
            operations = [dict(r) for r in self._conn.execute("SELECT * FROM desktop_operations")] if "desktop_operations" in tables else []
            translations = [dict(r) for r in self._conn.execute("SELECT * FROM task_translation")] if "task_translation" in tables else []
            attempts = [dict(r) for r in self._conn.execute("SELECT * FROM translation_attempts ORDER BY id")] if "translation_attempts" in tables else []
            for name in ("task_translation", "translation_attempts", "desktop_jobs", "desktop_operations", "tasks"):
                self._conn.execute(f"DROP TABLE IF EXISTS {name}")
            for sql in (
                """CREATE TABLE bilibili_accounts (
                    account_id TEXT PRIMARY KEY, uid TEXT NOT NULL UNIQUE,
                    nickname TEXT NOT NULL DEFAULT '', remark TEXT NOT NULL DEFAULT '',
                    lifecycle TEXT NOT NULL DEFAULT 'active', slot INTEGER,
                    credential_ref TEXT, credential_revision INTEGER NOT NULL DEFAULT 0,
                    auth_state TEXT NOT NULL DEFAULT 'unverified', verified_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(account_id,uid),
                    CHECK ((lifecycle='active' AND slot IS NOT NULL AND typeof(slot)='integer' AND slot IN (1,2,3,4,5))
                        OR (lifecycle='archived' AND slot IS NULL)))""",
                "CREATE UNIQUE INDEX uq_account_slot ON bilibili_accounts(slot) WHERE lifecycle='active'",
                """CREATE TRIGGER immutable_uid BEFORE UPDATE OF uid ON bilibili_accounts
                    WHEN OLD.uid IS NOT NEW.uid BEGIN SELECT RAISE(ABORT,'ACCOUNT_IDENTITY_IMMUTABLE'); END""",
            ):
                self._conn.execute(sql)
            columns = []
            for field in fields(Task):
                if field.name == "task_id":
                    columns.append("task_id TEXT PRIMARY KEY")
                elif field.name in ("revision", "metadata_revision", "cancel_requested"):
                    columns.append(f"{field.name} INTEGER NOT NULL DEFAULT 0")
                elif field.name in ("account_id", "account_uid_snapshot"):
                    columns.append(f"{field.name} TEXT")
                else:
                    columns.append(f"{field.name} TEXT NOT NULL DEFAULT ''")
            columns += ["FOREIGN KEY(account_id,account_uid_snapshot) REFERENCES bilibili_accounts(account_id,uid)",
                        "CHECK((account_id IS NULL) = (account_uid_snapshot IS NULL))"]
            self._conn.execute("CREATE TABLE tasks (" + ",".join(columns) + ")")
            for sql in (
                "CREATE UNIQUE INDEX uq_task_account ON tasks(video_id,account_id) WHERE account_id IS NOT NULL",
                "CREATE INDEX ix_task_video ON tasks(video_id)",
                "CREATE INDEX ix_task_account_status ON tasks(account_id,status)",
                """CREATE TRIGGER immutable_task_account BEFORE UPDATE OF account_id,account_uid_snapshot ON tasks
                    WHEN OLD.account_id IS NOT NULL AND (OLD.account_id IS NOT NEW.account_id OR OLD.account_uid_snapshot IS NOT NEW.account_uid_snapshot)
                    BEGIN SELECT RAISE(ABORT,'TASK_ACCOUNT_IMMUTABLE'); END""",
                "CREATE TABLE desktop_jobs(task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), payload TEXT NOT NULL)",
                "CREATE TABLE desktop_operations(id TEXT PRIMARY KEY, method TEXT NOT NULL, request_hash TEXT, result TEXT NOT NULL)",
                "CREATE TABLE legacy_operations(id TEXT PRIMARY KEY, method TEXT NOT NULL, result TEXT NOT NULL)",
                "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)",
                "CREATE TABLE import_conflicts(source_id TEXT,legacy_id TEXT,payload TEXT,existing_task_id TEXT,PRIMARY KEY(source_id,legacy_id))",
                "CREATE TABLE legacy_task_map(source_id TEXT, legacy_id TEXT, task_id TEXT REFERENCES tasks(task_id), PRIMARY KEY(source_id,legacy_id))",
                "CREATE TABLE queue_sequence(value INTEGER NOT NULL)",
                "INSERT INTO queue_sequence VALUES(0)",
                "CREATE TABLE upload_attempts(attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id), account_id TEXT NOT NULL, uid TEXT NOT NULL, run_id TEXT NOT NULL, prepared_at TEXT NOT NULL, command_started_at TEXT, ended_at TEXT, outcome TEXT NOT NULL, bv_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '')",
            ):
                self._conn.execute(sql)
            mapping = {}
            for row in old:
                row["task_id"] = str(uuid.uuid4())
                row["account_id"] = row["account_uid_snapshot"] = None
                row["work_root"] = str(Path(row["work_dir"]).parent) if row.get("work_dir") else ""
                if row.get("work_dir") and (not Path(row["work_dir"]).is_absolute() or Path(row["work_dir"]).name != row["video_id"]):
                    row["work_dir"] = row["work_root"] = row["video_path"] = row["cover_path"] = ""
                task = Task(**{k: v for k, v in row.items() if k in Task.__dataclass_fields__})
                self.upsert(task)
                mapping[task.video_id] = task.task_id
                self._conn.execute("INSERT INTO legacy_task_map VALUES('migration',?,?)", (task.video_id, task.task_id))
            for job in jobs:
                if job["video_id"] in mapping:
                    self._conn.execute("INSERT INTO desktop_jobs VALUES(?,?)", (mapping[job["video_id"]], job["payload"]))
            for op in operations:
                self._conn.execute("INSERT INTO legacy_operations VALUES(?,?,?)", (op["id"], op["method"], op["result"]))
            self._translation_schema()
            for row in translations:
                if row["video_id"] in mapping:
                    self._conn.execute("INSERT OR REPLACE INTO task_translation VALUES(?,?)", (mapping[row["video_id"]], row["payload"]))
            for row in attempts:
                if row["video_id"] in mapping:
                    self._conn.execute("INSERT INTO translation_attempts(task_id,payload) VALUES(?,?)", (mapping[row["video_id"]], row["payload"]))
            self._conn.execute("PRAGMA user_version=3")
            self._conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (3, _now()))
            if self._conn.execute("PRAGMA foreign_key_check").fetchone():
                raise Yt2BiliError("数据库迁移引用检查失败。")
            self._events.clear()

    def _translation_schema(self):
        from yt2bili.translation.config import legacy_snapshot
        self._conn.execute("CREATE TABLE IF NOT EXISTS task_translation(task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),payload TEXT NOT NULL)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS translation_attempts(id INTEGER PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(task_id),payload TEXT NOT NULL)")
        for row in self._conn.execute("SELECT task_id,payload FROM desktop_jobs").fetchall():
            job = json.loads(row["payload"])
            job["settings"] = legacy_snapshot(job.get("settings", {}))
            self._conn.execute("UPDATE desktop_jobs SET payload=? WHERE task_id=?", (json.dumps(job, ensure_ascii=False), row["task_id"]))
        for row in self._conn.execute("SELECT task_id,title_zh FROM tasks").fetchall():
            record = {"config_snapshot": legacy_snapshot({})}
            if row["title_zh"]:
                record.update(state="legacy_preserved", provider="unknown", user_edited=False)
            self._conn.execute("INSERT OR IGNORE INTO task_translation VALUES(?,?)", (row["task_id"], json.dumps(record)))

    def translation(self, identity):
        with self._lock:
            task = self.get(identity)
            row = self._conn.execute("SELECT payload FROM task_translation WHERE task_id=?", (task.task_id,)).fetchone() if task else None
            return json.loads(row[0]) if row else None

    def reset_translation(self, identity):
        with self.transaction() as conn:
            task = self.get(identity)
            if task:
                conn.execute("DELETE FROM task_translation WHERE task_id=?", (task.task_id,))

    def save_translation(self, task, record):
        with self.transaction() as conn:
            self.upsert(task)
            conn.execute("INSERT OR REPLACE INTO task_translation VALUES(?,?)", (task.task_id, json.dumps(record, ensure_ascii=False)))

    def translation_attempt(self, identity, attempts):
        with self.transaction() as conn:
            task_id = self.require(identity).task_id
            conn.execute("INSERT INTO translation_attempts(task_id,payload) VALUES(?,?)", (task_id, json.dumps({"time": _now(), "attempts": attempts})))
            conn.execute("DELETE FROM translation_attempts WHERE task_id=? AND id NOT IN (SELECT id FROM translation_attempts WHERE task_id=? ORDER BY id DESC LIMIT 20)", (task_id, task_id))

    @contextmanager
    def transaction(self):
        notifications = []
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
                if outer:
                    self._conn.commit()
                    notifications, self._events = self._events, []
            except BaseException:
                if outer:
                    self._conn.rollback()
                    self._events.clear()
                raise
            finally:
                self._depth -= 1
        if self.on_change:
            for event, payload in notifications:
                self.on_change(event, payload)

    def get(self, identity):
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (identity,)).fetchone()
            if row:
                return Task(**dict(row))
            rows = self._conn.execute("SELECT * FROM tasks WHERE video_id=?", (identity,)).fetchall()
            if len(rows) > 1:
                raise Yt2BiliError("同一视频有多个账号任务，请使用 task_id。")
            return Task(**dict(rows[0])) if rows else None

    def for_account(self, video_id, account_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE video_id=? AND account_id=?", (video_id, account_id)).fetchone()
            return Task(**dict(row)) if row else None

    def require(self, identity):
        task = self.get(identity)
        if task is None:
            raise Yt2BiliError(f"找不到任务 {identity}。")
        return task

    def list_all(self):
        with self._lock:
            return [Task(**dict(r)) for r in self._conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC,task_id")]

    def upsert(self, task):
        with self.transaction() as conn:
            if not task.task_id:
                task.task_id = str(uuid.uuid4())
            row = conn.execute("SELECT created_at,revision,cancel_requested FROM tasks WHERE task_id=?", (task.task_id,)).fetchone()
            if row and row[2]:
                task.cancel_requested = 1
                if task.status not in ("submitted", "submission_unknown", "cancelled", "failed"):
                    task.status = "cancel_requested"
            task.created_at = row[0] if row else task.created_at or _now()
            task.revision = row[1] + 1 if row else max(1, task.revision)
            task.updated_at = _now()
            values = asdict(task)
            names = list(values)
            updates = ",".join(f"{n}=excluded.{n}" for n in names if n != "task_id")
            conn.execute(f"INSERT INTO tasks({','.join(names)}) VALUES({','.join('?' for _ in names)}) ON CONFLICT(task_id) DO UPDATE SET {updates}", tuple(values.values()))
            job = self.get_job(task.task_id) or {}
            self._events.append(("task.status", {**values, "run_id": job.get("run_id")}))

    def update(self, task_id, **changes):
        with self.transaction():
            task = self.require(task_id)
            for key, value in changes.items():
                setattr(task, key, value)
            self.upsert(task)
            return task

    def next_sequence(self):
        with self.transaction() as conn:
            conn.execute("UPDATE queue_sequence SET value=value+1")
            return conn.execute("SELECT value FROM queue_sequence").fetchone()[0]

    def save_job(self, identity, payload):
        with self.transaction() as conn:
            task = self.require(identity)
            conn.execute("INSERT INTO desktop_jobs VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload", (task.task_id, json.dumps(payload, ensure_ascii=False)))

    def get_job(self, identity):
        with self._lock:
            task = self.get(identity)
            if not task:
                return None
            row = self._conn.execute("SELECT payload FROM desktop_jobs WHERE task_id=?", (task.task_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def operation(self, operation_id, method, result=None, request_hash=None):
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM legacy_operations WHERE id=?", (operation_id,)).fetchone():
                raise Yt2BiliError("旧版操作已登记，请刷新任务后使用新的操作 ID；不会再次执行。")
            row = conn.execute("SELECT * FROM desktop_operations WHERE id=?", (operation_id,)).fetchone()
            if row:
                if row["method"] != method or row["request_hash"] != request_hash:
                    raise Yt2BiliError("操作 ID 已用于不同的请求参数。")
                return json.loads(row["result"])
            if result is not None:
                conn.execute("INSERT INTO desktop_operations VALUES(?,?,?,?)", (operation_id, method, request_hash, json.dumps(result)))
            return result

    def accounts(self, archived=False):
        with self._lock:
            sql = "SELECT * FROM bilibili_accounts" + ("" if archived else " WHERE lifecycle='active'") + " ORDER BY slot,created_at"
            return [dict(r) for r in self._conn.execute(sql)]

    def account(self, account_id, active=True):
        with self._lock:
            row = self._conn.execute("SELECT * FROM bilibili_accounts WHERE account_id=?", (account_id,)).fetchone()
            if not row or (active and row["lifecycle"] != "active"):
                raise Yt2BiliError("请选择有效的 Bilibili 账号。")
            return dict(row)

    def update_account(self, account_id, **changes):
        allowed = {"nickname", "remark", "lifecycle", "slot", "credential_ref", "credential_revision", "auth_state", "verified_at"}
        if set(changes) - allowed:
            raise Yt2BiliError("不支持的账号字段。")
        with self.transaction() as conn:
            self.account(account_id, active=False)
            changes["updated_at"] = _now()
            conn.execute("UPDATE bilibili_accounts SET " + ",".join(f"{k}=?" for k in changes) + " WHERE account_id=?", (*changes.values(), account_id))

    def close(self):
        with self._lock:
            self._conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

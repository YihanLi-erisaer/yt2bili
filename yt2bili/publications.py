"""Durable per-platform outcomes. Parent status is a projection, never a receipt."""
import json
import uuid

from yt2bili.db import _now
from yt2bili.exceptions import Yt2BiliError

TERMINAL = {"submitted", "abandoned"}
INFLIGHT = {"uploading_media", "creating"}


def migrate(store):
    with store.transaction() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS douyin_accounts(
            account_id TEXT PRIMARY KEY, client_key TEXT NOT NULL, open_id TEXT NOT NULL,
            nickname TEXT NOT NULL, lifecycle TEXT NOT NULL DEFAULT 'active',
            auth_state TEXT NOT NULL DEFAULT 'valid', binding_revision INTEGER NOT NULL DEFAULT 1,
            UNIQUE(client_key,open_id))""")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_douyin_slot ON douyin_accounts(lifecycle) WHERE lifecycle='active'")
        db.execute("""CREATE TRIGGER IF NOT EXISTS immutable_douyin_identity BEFORE UPDATE OF client_key,open_id ON douyin_accounts
            BEGIN SELECT RAISE(ABORT,'ACCOUNT_IDENTITY_IMMUTABLE'); END""")
        db.execute("""CREATE TABLE IF NOT EXISTS task_publications(
            publication_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
            platform TEXT NOT NULL CHECK(platform IN ('bilibili','douyin')), account_id TEXT,
            source_video_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending_assets',
            revision INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL DEFAULT '',
            remote_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            retain_assets INTEGER NOT NULL DEFAULT 0, snapshot TEXT NOT NULL DEFAULT '{}',
            UNIQUE(task_id,platform))""")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_douyin_source ON task_publications(account_id,source_video_id) WHERE platform='douyin'")
        db.execute("""CREATE TRIGGER IF NOT EXISTS immutable_publication_identity
            BEFORE UPDATE OF platform,account_id,source_video_id,task_id ON task_publications
            WHEN OLD.platform IS NOT NEW.platform OR OLD.source_video_id IS NOT NEW.source_video_id
                OR OLD.task_id IS NOT NEW.task_id OR (OLD.account_id IS NOT NULL AND OLD.account_id IS NOT NEW.account_id)
            BEGIN SELECT RAISE(ABORT,'PUBLICATION_IDENTITY_IMMUTABLE'); END""")
        for task in store.list_all():
            ensure_bili(store, task)
        db.execute("PRAGMA user_version=4")
        db.execute("INSERT OR REPLACE INTO schema_migrations VALUES(4,?)", (_now(),))


def migrate_acfun(store):
    """Rebuild the platform CHECK without changing any existing publication identity."""
    with store.transaction() as db:
        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='task_publications'").fetchone()
        if schema and "'acfun'" in schema[0]:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='acfun_accounts'").fetchone():
                raise Yt2BiliError("AcFun 表结构不完整，请从迁移备份恢复。")
            db.execute("PRAGMA user_version=5")
            db.execute("INSERT OR REPLACE INTO schema_migrations VALUES(5,?)", (_now(),))
            return
        db.execute("""CREATE TABLE IF NOT EXISTS acfun_accounts(
            account_id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE,
            nickname TEXT NOT NULL, lifecycle TEXT NOT NULL DEFAULT 'active',
            auth_state TEXT NOT NULL DEFAULT 'valid', binding_revision INTEGER NOT NULL DEFAULT 1,
            adapter_version TEXT NOT NULL DEFAULT 'web-v1')""")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_acfun_slot ON acfun_accounts(lifecycle) WHERE lifecycle='active'")
        db.execute("""CREATE TRIGGER IF NOT EXISTS immutable_acfun_identity BEFORE UPDATE OF user_id ON acfun_accounts
            BEGIN SELECT RAISE(ABORT,'ACCOUNT_IDENTITY_IMMUTABLE'); END""")
        before = db.execute("SELECT count(*) FROM task_publications").fetchone()[0]
        db.execute("DROP TRIGGER IF EXISTS immutable_publication_identity")
        db.execute("""CREATE TABLE task_publications_v5(
            publication_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
            platform TEXT NOT NULL CHECK(platform IN ('bilibili','douyin','acfun')), account_id TEXT,
            source_video_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending_assets',
            revision INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL DEFAULT '',
            remote_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            retain_assets INTEGER NOT NULL DEFAULT 0, snapshot TEXT NOT NULL DEFAULT '{}',
            UNIQUE(task_id,platform))""")
        db.execute("INSERT INTO task_publications_v5 SELECT * FROM task_publications")
        if db.execute("SELECT count(*) FROM task_publications_v5").fetchone()[0] != before:
            raise Yt2BiliError("AcFun 迁移前后投稿记录数量不一致。")
        db.execute("DROP TABLE task_publications")
        db.execute("ALTER TABLE task_publications_v5 RENAME TO task_publications")
        db.execute("CREATE UNIQUE INDEX uq_douyin_source ON task_publications(account_id,source_video_id) WHERE platform='douyin'")
        db.execute("CREATE UNIQUE INDEX uq_acfun_source ON task_publications(account_id,source_video_id) WHERE platform='acfun'")
        db.execute("""CREATE TRIGGER immutable_publication_identity
            BEFORE UPDATE OF platform,account_id,source_video_id,task_id ON task_publications
            WHEN OLD.platform IS NOT NEW.platform OR OLD.source_video_id IS NOT NEW.source_video_id
                OR OLD.task_id IS NOT NEW.task_id OR (OLD.account_id IS NOT NULL AND OLD.account_id IS NOT NEW.account_id)
            BEGIN SELECT RAISE(ABORT,'PUBLICATION_IDENTITY_IMMUTABLE'); END""")
        db.execute("""CREATE TABLE IF NOT EXISTS acfun_attempts(
            attempt_id TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES task_publications(publication_id),
            phase TEXT NOT NULL, started_at TEXT NOT NULL, upload_task_id TEXT NOT NULL DEFAULT '',
            video_id TEXT NOT NULL DEFAULT '', create_intent_at TEXT NOT NULL DEFAULT '',
            douga_id TEXT NOT NULL DEFAULT '', media_sha256 TEXT NOT NULL DEFAULT '',
            request_sha256 TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '')""")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise Yt2BiliError("AcFun 迁移引用检查失败。")
        db.execute("PRAGMA user_version=5")
        db.execute("INSERT OR REPLACE INTO schema_migrations VALUES(5,?)", (_now(),))


def items(store, task_id):
    with store._lock:
        task = store.require(task_id)
        result = [dict(r) for r in store._conn.execute("SELECT * FROM task_publications WHERE task_id=? ORDER BY platform", (task.task_id,))]
        for p in result:
            snapshot = json.loads(p["snapshot"])
            p["account_label"] = task.account_name_snapshot if p["platform"] == "bilibili" else snapshot.get("nickname", p["account_id"])
        return result


def get(store, publication_id):
    with store._lock:
        row = store._conn.execute("SELECT * FROM task_publications WHERE publication_id=?", (publication_id,)).fetchone()
        if not row:
            raise Yt2BiliError("投稿目标不存在。")
        return dict(row)


def ensure_bili(store, task):
    with store.transaction() as db:
        status = task.status if task.status in {"ready", "submitted", "submission_unknown", "failed", "cancelled", "interrupted"} else "pending_assets"
        if task.status == "uploading": status = "submission_unknown"
        db.execute("INSERT OR IGNORE INTO task_publications(publication_id,task_id,platform,account_id,source_video_id,status,remote_id) VALUES(?,?,'bilibili',?,?,?,?)",
                   (str(uuid.uuid4()), task.task_id, task.account_id, task.video_id, status, task.bv_id))


def dual(store, task_id):
    return multi_target(store, task_id)


def multi_target(store, task_id):
    return len(items(store, task_id)) > 1


def change(store, publication_id, **values):
    if set(values) - {"status", "text", "remote_id", "error", "snapshot", "retain_assets"}:
        raise ValueError("Unsupported publication field")
    with store.transaction() as db:
        p = get(store, publication_id)
        db.execute("UPDATE task_publications SET " + ",".join(f"{k}=?" for k in values) + ",revision=revision+1 WHERE publication_id=?", (*values.values(), publication_id))
        store._events.append(("publication.changed", {"task_id": p["task_id"], "publication_id": publication_id}))


def for_platform(store, task_id, platform):
    return next((p for p in items(store, task_id) if p["platform"] == platform), None)


def project(store, task_id):
    with store.transaction():
        pubs = items(store, task_id)
        states = {p["status"] for p in pubs}
        if "submission_unknown" in states: status = "submission_unknown"
        elif states <= TERMINAL: status = "submitted" if states == {"submitted"} else "completed_with_abandon"
        elif "submitted" in states: status = "partial_success"
        elif states & INFLIGHT: status = "uploading"
        elif states & {"queued", "waiting"}: status = "queued_upload"
        elif states <= {"ready", "blocked_validation"} and "ready" in states: status = "ready"
        elif "failed" in states or "blocked_validation" in states: status = "failed"
        elif "interrupted" in states: status = "interrupted"
        elif "cancelled" in states: status = "cancelled"
        else: return
        store._conn.execute("UPDATE tasks SET cancel_requested=0 WHERE task_id=?", (store.require(task_id).task_id,))
        store.update(task_id, status=status, error="；".join(p["platform"] + ": " + p["error"] for p in pubs if p["error"]))


def can_cleanup(store, task_id):
    pubs = items(store, task_id)
    return bool(pubs) and all(p["status"] in TERMINAL and not p["retain_assets"] for p in pubs)


def prepare(store, task_id):
    task = store.require(task_id)
    for p in items(store, task_id):
        if p["status"] in TERMINAL | {"submission_unknown"}: continue
        values = {"status": "ready", "error": "", "text": p["text"] or task.title_zh}
        if p["platform"] == "acfun":
            snapshot = json.loads(p["snapshot"])
            snapshot.setdefault("title", task.title_zh)
            snapshot.setdefault("description", task.desc_zh)
            values["snapshot"] = json.dumps(snapshot, ensure_ascii=False)
        change(store, p["publication_id"], **values)


def freeze(store, task_id, targets=None):
    task = store.require(task_id)
    selected = set(targets) if targets is not None else None
    with store.transaction():
        for p in items(store, task_id):
            if selected is not None and p["publication_id"] not in selected: continue
            if p["status"] != "ready": continue
            existing = json.loads(p["snapshot"])
            change(store, p["publication_id"], status="queued", snapshot=json.dumps({**existing,
                "title": existing.get("title", task.title_zh) if p["platform"] == "acfun" else task.title_zh,
                "description": existing.get("description", task.desc_zh) if p["platform"] == "acfun" else task.desc_zh, "text": p["text"],
                "account_id": p["account_id"], "metadata_revision": task.metadata_revision,
                "revision": p["revision"]}, ensure_ascii=False))

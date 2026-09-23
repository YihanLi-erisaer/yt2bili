"""Both previously released schema-v2 shapes converge without losing identity."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from yt2bili.db import Task, TaskStore
from yt2bili.translation import tasks
from yt2bili.translation.config import DEFAULTS
from yt2bili.translation.types import TranslationResult
from types import SimpleNamespace


class JointMigrationTests(unittest.TestCase):
    def test_translation_v2_preserves_edits_policy_attempts_and_legacy_operations(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tasks.sqlite"
            with sqlite3.connect(path) as conn:
                conn.executescript('''
                    CREATE TABLE tasks(video_id TEXT PRIMARY KEY,url TEXT,status TEXT,title_zh TEXT,desc_zh TEXT);
                    CREATE TABLE desktop_jobs(video_id TEXT PRIMARY KEY,payload TEXT);
                    CREATE TABLE desktop_operations(id TEXT PRIMARY KEY,method TEXT,result TEXT);
                    CREATE TABLE task_translation(video_id TEXT PRIMARY KEY,payload TEXT);
                    CREATE TABLE translation_attempts(id INTEGER PRIMARY KEY,video_id TEXT,payload TEXT);
                    PRAGMA user_version=2;
                ''')
                record = {"state": "edited", "user_edited": True, "provider": "local_llm", "config_snapshot": DEFAULTS}
                conn.execute("INSERT INTO tasks VALUES('abcdefghijk','url','ready','人工标题','人工简介')")
                conn.execute("INSERT INTO desktop_jobs VALUES(?,?)", ("abcdefghijk", json.dumps({"settings": DEFAULTS, "mode": "preview"})))
                conn.execute("INSERT INTO desktop_operations VALUES('op','tasks.create','{}')")
                conn.execute("INSERT INTO task_translation VALUES(?,?)", ("abcdefghijk", json.dumps(record)))
                conn.execute("INSERT INTO translation_attempts VALUES(1,?,?)", ("abcdefghijk", '{"attempts":[{"code":"FAILED"}]}'))
            conn.close()
            store = TaskStore(path)
            try:
                task = store.require("abcdefghijk")
                self.assertNotEqual(task.task_id, task.video_id)
                self.assertIsNone(task.account_id)
                self.assertEqual((task.title_zh, task.desc_zh), ("人工标题", "人工简介"))
                self.assertEqual(store.translation(task.task_id), record)
                self.assertEqual(store.get_job(task.task_id)["settings"]["translation_primary"], "local_llm")
                self.assertEqual(store._conn.execute("SELECT task_id FROM translation_attempts").fetchone()[0], task.task_id)
                self.assertEqual(store._conn.execute("SELECT id FROM legacy_operations").fetchone()[0], "op")
                self.assertEqual(store._conn.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(store._conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertTrue(Path(str(path) + ".pre-v3.bak").exists())
            finally:
                store.close()

    def test_multi_account_v2_keeps_task_ids_and_old_deepl_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tasks.sqlite"
            store = TaskStore(path)
            task = Task("abcdefghijk", "url", "ready", title_zh="既有译文")
            store.upsert(task)
            store.save_job(task.task_id, {"settings": {"bili_tid": 171}})
            store._conn.executescript("DROP TABLE task_translation; DROP TABLE translation_attempts; PRAGMA user_version=2;")
            store.close()
            store = TaskStore(path)
            try:
                self.assertEqual(store.require(task.task_id).task_id, task.task_id)
                self.assertEqual(store.translation(task.task_id)["state"], "legacy_preserved")
                self.assertEqual(store.get_job(task.task_id)["settings"]["translation_primary"], "deepl")
                self.assertFalse(store.get_job(task.task_id)["settings"]["translation_fallback_enabled"])
            finally:
                store.close()

    def test_same_video_translation_and_attempts_are_task_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = TaskStore(root / "tasks.sqlite")
            try:
                first, second = [Task("abcdefghijk", "url", "ready", title_zh=title) for title in ("账号一", "账号二")]
                for task in (first, second):
                    store.save_translation(task, {"state": "edited", "user_edited": True})
                meta = SimpleNamespace(title="Source", description="Text", language="en", uploader="Author", webpage_url="url")
                settings = SimpleNamespace(**DEFAULTS, title_limit=80, desc_limit=2000)
                with patch.object(tasks, "translate_group", return_value=TranslationResult("新的译文", "正文", "local_llm")):
                    tasks.prepare(settings, store, first, meta, root, force=True)
                self.assertEqual(store.require(second.task_id).title_zh, "账号二")
                self.assertTrue(store.translation(second.task_id)["user_edited"])
                self.assertEqual(store.translation(first.task_id)["provider"], "local_llm")
                self.assertEqual(store._conn.execute("SELECT task_id FROM translation_attempts").fetchone()[0], first.task_id)
            finally:
                store.close()

    def test_early_multi_account_v2_without_bookkeeping_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tasks.sqlite"
            store = TaskStore(path)
            task = Task("abcdefghijk", "url", "submitted", title_zh="保留标题", bv_id="BV-existing")
            store.upsert(task)
            store.save_job(task.task_id, {"settings": {"bili_tid": 171}, "mode": "preview"})
            before = dict(store._conn.execute("SELECT * FROM tasks").fetchone())
            store._conn.executescript("""
                DROP TABLE schema_migrations;
                DROP TABLE import_conflicts;
                DROP TABLE task_translation;
                DROP TABLE translation_attempts;
                PRAGMA user_version=2;
            """)
            store.close()
            for _ in range(2):  # Migration and subsequent normal startup both work.
                store = TaskStore(path)
                try:
                    self.assertEqual(dict(store._conn.execute("SELECT * FROM tasks").fetchone()), before)
                    self.assertEqual(store._conn.execute("PRAGMA user_version").fetchone()[0], 3)
                    self.assertEqual(store._conn.execute("SELECT version FROM schema_migrations").fetchone()[0], 3)
                    self.assertEqual(store._conn.execute("SELECT * FROM import_conflicts").fetchall(), [])
                    self.assertEqual(store._conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(store.get_job(task.task_id)["settings"]["translation_primary"], "deepl")
                    self.assertEqual(store.translation(task.task_id)["state"], "legacy_preserved")
                finally:
                    store.close()
            with closing(sqlite3.connect(str(path) + ".pre-v3.bak")) as backup:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(backup.execute("SELECT task_id,bv_id FROM tasks").fetchone(), (task.task_id, task.bv_id))

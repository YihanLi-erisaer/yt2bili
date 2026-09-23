from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_desktop as desktop_tests
from test_desktop import MemoryVault, login_fixture
from yt2bili import bili_upload, desktop_auth, events, media, pipeline
from yt2bili.cli import _build_parser
from yt2bili.db import Task, TaskStore
from yt2bili.desktop_service import DesktopService
from yt2bili.desktop_settings import atomic_json
from yt2bili.exceptions import Yt2BiliError
from yt2bili.identity import parse_single_video_url
from yt2bili.locking import account_guard, coordination_dir
from yt2bili.paths import AppPaths
from yt2bili.process_manager import process_alive


def credentials(uid):
    info = login_fixture()
    info["token_info"]["mid"] = int(uid)
    next(c for c in info["cookie_info"]["cookies"] if c["name"] == "DedeUserID")["value"] = str(uid)
    return info


class MultiAccountTests(unittest.TestCase):
    setUp = desktop_tests.DesktopTests.setUp
    mocks = desktop_tests.DesktopTests.mocks
    meta = desktop_tests.DesktopTests.meta
    download = desktop_tests.DesktopTests.download
    prepare = desktop_tests.DesktopTests.prepare
    upload = desktop_tests.DesktopTests.upload
    wait_idle = desktop_tests.DesktopTests.wait_idle
    create = desktop_tests.DesktopTests.create

    def add_account(self, uid):
        return self.service.accounts.bind(credentials(uid), verify=False)

    def test_capacity_archival_restore_and_database_constraints(self):
        accounts = [self.account] + [self.add_account(i) for i in range(124, 128)]
        with self.assertRaises(Yt2BiliError): self.add_account(128)
        with self.assertRaises(Yt2BiliError): self.add_account(123)
        self.service.accounts.clear(self.account["account_id"])
        with self.assertRaises(Yt2BiliError): self.add_account(128)
        self.service.accounts.archive(self.account["account_id"])
        restored = self.add_account(123)
        self.assertEqual(restored["account_id"], self.account["account_id"])
        self.assertEqual(len(self.service.scheduler.upload_lanes), 5)
        with self.assertRaises(sqlite3.IntegrityError), self.service.store.transaction() as conn:
            conn.execute("UPDATE bilibili_accounts SET slot=6 WHERE account_id=?", (accounts[-1]["account_id"],))

    def test_concurrent_add_for_fifth_slot_commits_only_one(self):
        for i in range(124, 127): self.add_account(i)
        barrier = threading.Barrier(2)
        def add(uid):
            barrier.wait()
            try: return self.add_account(uid)
            except Yt2BiliError: return None
        with ThreadPoolExecutor(2) as pool:
            result = list(pool.map(add, [127, 128]))
        self.assertEqual(sum(r is not None for r in result), 1)
        self.assertEqual(len(self.service.accounts.list()), 5)
        self.assertEqual(len(list((self.paths.root / "secrets/bilibili").glob("*/credentials/*.json"))), 5)

    def test_wrong_uid_cannot_replace_credentials(self):
        original = self.service.store.account(self.account["account_id"])
        with self.assertRaises(Yt2BiliError):
            self.service.accounts.bind(credentials(999), self.account["account_id"], verify=False)
        self.assertEqual(self.service.store.account(self.account["account_id"])["credential_ref"], original["credential_ref"])
        info = credentials(123)
        info["token_info"]["mid"] = 124
        with self.assertRaises(Yt2BiliError): self.service.accounts.bind(info, verify=False)

    def test_identity_same_video_five_accounts_and_cleanup_isolated(self):
        accounts = [self.account] + [self.add_account(i) for i in range(124, 128)]
        with self.mocks():
            results = [self.service.create("https://youtu.be/abcdefghijk", f"create-op-{i}", a["account_id"]) for i,a in enumerate(accounts)]
            self.wait_idle()
            ids = [r["task_id"] for r in results]
            self.assertEqual(len(set(ids)), 5)
            tasks = [self.service.task(t) for t in ids]
            self.assertEqual(len({t.work_dir for t in tasks}), 5)
            with self.assertRaises(Yt2BiliError): self.service.task("abcdefghijk")
            self.service.update_metadata(ids[0], "独立标题", "独立简介")
            self.assertEqual(self.service.task(ids[1]).title_zh, "翻译标题")
            self.service.submit(ids[0], "submit-one")
            self.wait_idle()
            self.assertFalse(Path(tasks[0].work_dir).exists())
            self.assertTrue(all(Path(t.video_path).exists() for t in tasks[1:]))
            with self.assertRaises(Yt2BiliError): self.service.bind_legacy(ids[1], accounts[2]["account_id"])
            with self.assertRaises(sqlite3.IntegrityError):
                self.service.store.update(ids[1], account_id=accounts[2]["account_id"], account_uid_snapshot=accounts[2]["uid"])

    def test_five_uploads_overlap_and_each_account_serializes_two_jobs(self):
        accounts = [self.account] + [self.add_account(i) for i in range(124, 128)]
        entered = threading.Barrier(6)
        release = threading.Event()
        lock = threading.Lock()
        running, peak, calls = {}, {}, []
        def upload(settings, *args, on_started=None):
            on_started(99999999)
            uid = settings.account_uid
            with lock:
                running[uid] = running.get(uid, 0) + 1
                peak[uid] = max(peak.get(uid, 0), running[uid])
                first = uid not in calls
                calls.append(uid)
            if first:
                entered.wait(8)
                release.wait(8)
            with lock: running[uid] -= 1
            return "BV1234567890"
        with self.mocks(), patch.object(bili_upload, "upload", side_effect=upload):
            ids = [self.service.create("https://youtu.be/" + video, f"p-{n}-{video}", a["account_id"])["task_id"]
                   for n,a in enumerate(accounts) for video in ("abcdefghijk", "12345678901")]
            self.wait_idle()
            try:
                for t in ids: self.service.submit(t, "submit-" + t)
                entered.wait(8)
                with lock:
                    self.assertEqual(sum(running.values()), 5)
                    self.assertEqual(len(calls), 5)
                status = self.service.prepare_shutdown()
                self.assertFalse(status["ready"])
            finally: release.set()
            self.wait_idle()
            self.assertEqual(peak, {a["uid"]: 1 for a in accounts})
            self.assertEqual(sum(t.status == "submitted" for t in self.service.store.list_all()), 5)
            self.assertEqual(sum(t.status == "cancelled" for t in self.service.store.list_all()), 5)
            self.assertTrue(self.service.scheduler.shutdown_status()["ready"])

    def test_expired_head_does_not_block_preview_or_other_account(self):
        other = self.add_account(124)
        with self.mocks():
            first = self.create()["task_id"]
            self.wait_idle()
            self.service.accounts.clear(self.account["account_id"])
            self.service.submit(first, "waiting-login")
            preview = self.create("12345678901")["task_id"]
            second = self.service.create("https://youtu.be/abcdefghijk", "other-account", other["account_id"], "auto")["task_id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.service.task(preview).status == "ready" and self.service.task(second).status == "submitted": break
                time.sleep(.01)
            self.assertEqual(self.service.task(first).wait_reason, "auth_required")
            self.assertEqual(self.service.task(preview).status, "ready")
            self.assertEqual(self.service.task(second).status, "submitted")
            self.service.accounts.bind(credentials(123), self.account["account_id"], verify=False)
            self.wait_idle()
            self.assertEqual(self.service.task(first).status, "submitted")

    def test_operation_parameter_conflict_and_transaction_rollback(self):
        with self.mocks():
            result = self.create()
            self.wait_idle()
            with self.assertRaises(Yt2BiliError):
                self.service.create("https://youtu.be/12345678901", "create-abcdefghijk", self.account["account_id"])
            original = self.service.store.operation
            def fail(op, method, result=None, **kwargs):
                if result is not None: raise OSError("commit result failed")
                return original(op, method, result, **kwargs)
            with patch.object(self.service.store, "operation", side_effect=fail), self.assertRaises(OSError):
                self.create("12345678901")
            self.assertIsNone(self.service.store.get("12345678901"))
            self.assertEqual(self.create(), result)

    def test_cancel_before_popen_and_late_stage_write_cannot_resurrect(self):
        entered, release = threading.Event(), threading.Event()
        def renew(settings):
            entered.set(); release.wait(4)
        with self.mocks():
            tid = self.create()["task_id"]
            self.wait_idle()
            stale = self.service.task(tid)
            with patch.object(bili_upload, "renew", side_effect=renew), patch.object(bili_upload, "upload") as upload:
                try:
                    self.service.submit(tid, "submit-cancel")
                    self.assertTrue(entered.wait(3))
                    self.service.cancel(tid)
                    stale.status = "ready"
                    self.service.store.upsert(stale)
                    self.assertEqual(self.service.task(tid).status, "cancel_requested")
                finally: release.set()
                self.wait_idle()
                upload.assert_not_called()
            self.assertEqual(self.service.task(tid).status, "cancelled")

    def test_uid_cooldown_and_corrupt_marker_do_not_start_upload(self):
        with self.mocks():
            tid = self.create()["task_id"]
            self.wait_idle()
        task = self.service.task(tid)
        item = SimpleNamespace(task_id=tid, settings=replace(self.service.config.build(), upload_gap_seconds=20))
        marker = coordination_dir("123") / "attempt-state.json"
        atomic_json(marker, {"ended": 100})
        with patch("yt2bili.upload_coordinator.time.time", return_value=105), patch("yt2bili.upload_coordinator.time.monotonic", return_value=20):
            self.assertEqual(self.service.scheduler.uploader.try_submit(item), ("cooldown", 35))
        marker.write_text("invalid", encoding="utf-8")
        with patch("yt2bili.upload_coordinator.time.time", return_value=105), patch("yt2bili.upload_coordinator.time.monotonic", return_value=20):
            self.assertEqual(self.service.scheduler.uploader.try_submit(item), ("cooldown", 40))
        atomic_json(marker, {"inflight": "crash", "owner_pid": os.getpid()})
        with self.assertRaises(Yt2BiliError): self.service.resume_uploads(self.account["account_id"], True)

    def test_shared_profile_rejects_second_executor(self):
        with self.assertRaises(Yt2BiliError): DesktopService(self.paths, lambda *args: None, MemoryVault())

    def test_persisted_download_jobs_are_dispatched_in_fifo_order(self):
        from yt2bili import youtube
        order = []
        def download(url, *args, **kwargs):
            order.append(parse_single_video_url(url)[0])
            return self.download(url, *args, **kwargs)
        videos = [str(n) * 11 for n in range(1, 5)]
        with self.mocks(), patch.object(youtube, "download_video", side_effect=download):
            with self.service.scheduler.guard:
                for video in videos: self.create(video)
            self.wait_idle()
        self.assertEqual(order, videos)

    def test_upload_process_is_reaped_when_started_callback_fails(self):
        pids = []
        def failed(pid):
            pids.append(pid)
            raise OSError("attempt persistence failed")
        with self.assertRaises(OSError):
            bili_upload._run_logged([sys.executable, "-c", "import time; time.sleep(60)"], on_started=failed)
        self.assertEqual(len(pids), 1)
        self.assertFalse(process_alive(pids[0]))

    def test_late_cancel_at_preview_commit_settles_as_cancelled(self):
        original = self.service.store.update
        def update(identity, **changes):
            if changes.get("status") == "ready":
                original(identity, cancel_requested=1, status="cancel_requested")
            return original(identity, **changes)
        with self.mocks(), patch.object(self.service.store, "update", side_effect=update):
            task_id = self.create()["task_id"]
            self.wait_idle()
        self.assertEqual(self.service.task(task_id).status, "cancelled")

    def test_import_copies_material_and_preserves_source_and_identity(self):
        source = self.paths.root / "import-source"
        source.mkdir()
        old = TaskStore(source / "data/tasks.sqlite")
        work = source / "work/abcdefghijk"
        work.mkdir(parents=True)
        (work / "video.mp4").write_bytes(b"source material")
        old.upsert(Task("abcdefghijk", "https://youtu.be/abcdefghijk", "ready", work_dir=str(work), video_path=str(work / "video.mp4"), work_root=str(work.parent)))
        # A v1 fixture exercises video-id directories; v2 uses task-id directories.
        task = old.require("abcdefghijk")
        renamed = work.parent / task.task_id
        work.rename(renamed)
        old.update(task.task_id, work_dir=str(renamed), video_path=str(renamed / "video.mp4"))
        old.close()
        first = self.service.import_data(str(source))
        self.assertEqual(first["imported"], 1)
        imported = self.service.task("abcdefghijk")
        self.assertIsNone(imported.account_id)
        self.assertNotEqual(Path(imported.work_dir), renamed)
        self.assertEqual(Path(imported.video_path).read_bytes(), b"source material")
        self.assertTrue((renamed / "video.mp4").exists())
        self.assertEqual(self.service.import_data(str(source))["imported"], 0)

    @unittest.skipUnless(os.name == "nt", "Windows release process ownership")
    def test_parent_termination_reaps_real_windows_child(self):
        code = "from yt2bili.process_manager import own_children\nimport subprocess,sys,time\nwith own_children():\n p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n print(p.pid,flush=True)\n time.sleep(60)"
        parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        child = None
        try:
            child = int(parent.stdout.readline().strip())
            self.assertTrue(process_alive(child))
            parent.kill()
            parent.communicate(timeout=10)
            deadline = time.monotonic() + 5
            while process_alive(child) and time.monotonic() < deadline: time.sleep(.05)
            self.assertFalse(process_alive(child))
        finally:
            if parent.poll() is None: parent.kill()
            parent.communicate(timeout=10)

    def test_real_process_uid_lock_different_uid_and_release(self):
        code = "from yt2bili.locking import account_guard\nimport sys\nwith account_guard(sys.argv[1]): print('locked', flush=True); sys.stdin.readline()"
        process = subprocess.Popen([sys.executable, "-c", code, "123"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaises(Yt2BiliError):
                with account_guard("123"): pass
            with account_guard("124"): pass
        finally:
            process.communicate("\n", timeout=10)
        with account_guard("123"): pass


class InputAndMigrationTests(unittest.TestCase):
    def test_single_url_all_supported_forms_and_no_batch(self):
        for path in ("https://youtu.be/abcdefghijk", "https://youtube.com/watch?v=abcdefghijk&list=foo", "https://youtube.com/shorts/abcdefghijk", "https://youtube.com/live/abcdefghijk"):
            self.assertEqual(parse_single_video_url(path)[0], "abcdefghijk")
        for value in ("https://youtu.be/abcdefghijk\nhttps://youtu.be/abcdefghijk", "https://youtube.com/watch?v=abcdefghijk&v=12345678901", "https://youtube.com.evil/watch?v=abcdefghijk", "abcdefghijk", "https://youtube.com/playlist?list=foo"):
            with self.assertRaises(Yt2BiliError): parse_single_video_url(value)

    def test_cli_rejects_missing_account_batch_force_and_file(self):
        parser = _build_parser()
        with patch("sys.stderr", io.StringIO()):
            for args in (["run", "https://youtu.be/abcdefghijk"], ["run", "a", "b", "--account", "x"], ["run", "a", "--account", "x", "--force"], ["run", "--file", "a", "--account", "x"]):
                with self.assertRaises(SystemExit): parser.parse_args(args)
        args = parser.parse_args(["run", "https://youtu.be/abcdefghijk", "--account", "x"])
        self.assertFalse(args.auto)

    def test_v1_migration_preserves_history_jobs_and_old_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.sqlite"
            conn = sqlite3.connect(path)
            conn.executescript("CREATE TABLE tasks(video_id TEXT PRIMARY KEY,url TEXT,status TEXT,title_zh TEXT,bv_id TEXT,work_dir TEXT); CREATE TABLE desktop_jobs(video_id TEXT PRIMARY KEY,payload TEXT); CREATE TABLE desktop_operations(id TEXT PRIMARY KEY,method TEXT,result TEXT); PRAGMA user_version=1;")
            conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", ("abcdefghijk", "url", "submitted", "手工标题", "BV1234567890", str(Path(directory)/"work/abcdefghijk")))
            conn.execute("INSERT INTO desktop_jobs VALUES(?,?)", ("abcdefghijk", '{"mode":"auto"}'))
            conn.execute("INSERT INTO desktop_operations VALUES(?,?,?)", ("old-op", "tasks.create", '{"added":["abcdefghijk"]}'))
            conn.commit(); conn.close()
            store = TaskStore(path)
            try:
                task = store.require("abcdefghijk")
                self.assertIsNone(task.account_id)
                self.assertEqual((task.title_zh, task.bv_id), ("手工标题", "BV1234567890"))
                self.assertEqual(store.get_job(task.task_id)["mode"], "auto")
                with self.assertRaises(Yt2BiliError): store.operation("old-op", "tasks.create", request_hash="new")
                self.assertTrue(Path(str(path)+".pre-multi-account.bak").is_file())
            finally: store.close()

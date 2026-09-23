from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from yt2bili import events, media, pipeline, youtube, bili_upload, desktop_auth
from yt2bili.db import Task, TaskStore
from yt2bili.desktop_auth import LoginSession, validate_login
from yt2bili.desktop_service import DesktopService, parse_urls
from yt2bili.desktop_settings import DesktopSettings
from yt2bili.desktop_worker import Protocol, redact
from yt2bili.exceptions import InvalidMediaError, Yt2BiliError
from yt2bili.locking import FileLock, work_lock
from yt2bili.paths import AppPaths


# Windows CI flushes many small SQLite transactions more slowly than a local SSD.
# These tests assert ordering/state, not a five-second throughput guarantee.
ASYNC_TIMEOUT = 30


class MemoryVault:
    def __init__(self): self.values = {}
    def get_password(self, service, name): return self.values.get((service, name))
    def set_password(self, service, name, value): self.values[service, name] = value
    def delete_password(self, service, name): self.values.pop((service, name), None)


def login_fixture():
    return {"cookie_info": {"cookies": [{"name": name, "value": "123" if name == "DedeUserID" else "fixture"} for name in ("SESSDATA", "bili_jct", "DedeUserID")]},
            "sso": [], "token_info": {"access_token": "fixture", "refresh_token": "fixture", "expires_in": 3600, "mid": 123}}


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environment = patch.dict(os.environ, {"YT2BILI_COORDINATION_DIR": str(Path(self.tmp.name) / "coordination")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.paths = AppPaths.default(self.tmp.name, self.tmp.name)
        self.notifications = []
        self.service = DesktopService(self.paths, lambda event, payload: self.notifications.append((event, payload)), MemoryVault())
        self.addCleanup(self.service.close)
        self.service.config.set_key("test-key")
        self.uploads = []
        self.account = self.service.accounts.bind(login_fixture(), verify=False)
        self.service.config.values["upload_gap_seconds"] = 0

    def wait_until(self, predicate, description):
        deadline = time.monotonic() + ASYNC_TIMEOUT
        while not predicate():
            if time.monotonic() >= deadline:
                tasks = [(t.task_id, t.status, t.wait_reason, t.error)
                         for t in self.service.store.list_all()]
                self.fail(f"Timed out waiting for {description}; tasks={tasks}; "
                          f"queue={self.service.scheduler.snapshot()}")
            time.sleep(.05)

    def wait_idle(self):
        self.wait_until(lambda: not self.service.scheduler.snapshot()["active"], "idle scheduler")

    def meta(self, url, settings):
        video_id = parse_urls(url)[0][0]
        return youtube.YoutubeMeta(video_id, url, "Original title", "description", "author", 10, "thumb", None, url)

    def download(self, url, folder, settings, **kwargs):
        file = folder / "source.mp4"
        file.write_bytes(b"complete video")
        return file

    def prepare(self, settings, store, task, meta, work, video):
        task.title_zh = task.title_zh or "翻译标题"
        task.desc_zh = "简介"
        cover = work / "cover.jpg"
        cover.write_bytes(b"cover")
        task.cover_path = str(cover)
        store.upsert(task)

    def upload(self, settings, video, cover, title, desc, url, on_started=None):
        if on_started: on_started(99999999)
        self.uploads.append((video.parent.name, title, desc))
        return "BV1234567890"

    def mocks(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(media, "require_ffmpeg"))
        stack.enter_context(patch.object(youtube, "fetch_meta", side_effect=self.meta))
        stack.enter_context(patch.object(youtube, "download_video", side_effect=self.download))
        stack.enter_context(patch.object(media, "prepare_upload_video", side_effect=lambda source, *a, **kw: source))
        stack.enter_context(patch.object(pipeline, "_prepare_assets", side_effect=self.prepare))
        stack.enter_context(patch.object(bili_upload, "upload", side_effect=self.upload))
        stack.enter_context(patch.object(bili_upload, "renew"))
        stack.enter_context(patch.object(desktop_auth, "verify_credentials", side_effect=lambda info: {"uid": desktop_auth.credential_identity(info), "nickname": "Test"}))
        return stack

    def create(self, video_id="abcdefghijk", **kwargs):
        return self.service.create("https://youtu.be/" + video_id, "create-" + video_id, account_id=self.account["account_id"], **kwargs)

    def test_preview_never_submits_and_edit_is_used_on_submit(self):
        with self.mocks():
            self.create()
            self.wait_idle()
            self.assertFalse(self.uploads)
            task = self.service.task("abcdefghijk")
            self.assertEqual(task.status, "ready")
            self.service.update_metadata(task.video_id, "编辑后的标题", "编辑后的简介")
            self.service.config.build().bili_cookies.write_text("{}")
            self.service.submit(task.video_id, "submit-operation")
            self.service.submit(task.video_id, "submit-operation")
            self.wait_idle()
            self.assertEqual(len(self.uploads), 1)
            self.assertEqual(self.uploads[0][1], "编辑后的标题")
            self.assertIn("原链接：", self.uploads[0][2])
            self.assertEqual(self.service.task(task.video_id).status, "submitted")
            self.assertFalse(Path(task.work_dir).exists())

    def test_duplicate_operation_and_url_only_enqueues_once(self):
        with self.mocks():
            first = self.create()
            second = self.create()
            self.wait_idle()
            self.assertEqual(first, second)
            self.assertEqual(len(self.service.store.list_all()), 1)
            result = self.service.create("https://youtube.com/watch?v=abcdefghijk&t=20", "another-operation", account_id=self.account["account_id"])
            self.assertFalse(result["created"])

    def test_external_work_lock_prevents_retry(self):
        with self.mocks():
            self.create()
            self.wait_idle()
            task = self.service.task("abcdefghijk")
            self.service.store.update(task.task_id, status="failed")
            with work_lock(Path(task.work_dir)):
                self.service.retry(task.task_id, "locked-retry")
                self.wait_idle()
            self.assertEqual(self.service.task(task.task_id).status, "failed")

    def test_export_includes_rotated_logs_and_redacts_credentials(self):
        (self.paths.root / "logs/desktop.log.1").write_text("old task complete\nSESSDATA=secret", encoding="utf-8")
        (self.paths.root / "logs/desktop.log").write_text("new task complete", encoding="utf-8")
        target = self.paths.root / "export.txt"
        self.service.log_export(str(target))
        value = target.read_text(encoding="utf-8")
        self.assertIn("old task complete", value)
        self.assertIn("new task complete", value)
        self.assertNotIn("secret", value)

    def test_unknown_submission_cannot_retry_or_resubmit(self):
        with self.mocks():
            self.create()
            self.wait_idle()
            self.service.config.build().bili_cookies.write_text("{}")
            with patch.object(bili_upload, "upload", side_effect=lambda *a, **k: (k["on_started"](99999999), "")[1]):
                self.service.submit("abcdefghijk", "submit-unknown")
                self.wait_idle()
            task = self.service.task("abcdefghijk")
            self.assertEqual(task.status, "submission_unknown")
            self.assertTrue(Path(task.video_path).exists())
            with self.assertRaises(Yt2BiliError): self.service.retry(task.video_id, "retry-unknown")
            with self.assertRaises(Yt2BiliError): self.service.submit(task.video_id, "submit-again")
            self.service.resolve(task.video_id, bv_id="BV1234567890")
            self.assertTrue(Path(task.video_path).exists())

    def test_upload_error_becomes_unknown_not_automatic_retry(self):
        def fail_upload(*args, **kwargs):
            kwargs["on_started"](99999999)
            raise Yt2BiliError("connection lost")
        with self.mocks():
            self.create()
            self.wait_idle()
            self.service.config.build().bili_cookies.write_text("{}")
            with patch.object(bili_upload, "upload", side_effect=fail_upload):
                self.service.submit("abcdefghijk", "submit-lost")
                self.wait_idle()
            self.assertEqual(self.service.task("abcdefghijk").status, "submission_unknown")

    def test_four_tasks_continue_downloading_while_validation_waits(self):
        validating = threading.Event()
        release = threading.Event()
        fourth = threading.Event()
        original = self.download
        def download(url, *args, **kwargs):
            if "44444444444" in url: fourth.set()
            return original(url, *args, **kwargs)
        def validate(source, *args, **kwargs):
            if not validating.is_set():
                validating.set(); release.wait(4)
            return source
        with self.mocks(), patch.object(youtube, "download_video", side_effect=download), patch.object(media, "prepare_upload_video", side_effect=validate):
            try:
                for i in range(1, 5): self.create(str(i) * 11)
                self.assertTrue(validating.wait(2))
                self.assertTrue(fourth.wait(2))
            finally:
                release.set()
            self.wait_idle()
            self.assertTrue(all(t.status == "ready" for t in self.service.store.list_all()))

    def test_cancel_download_is_recoverable(self):
        started = threading.Event()
        def download(*args, **kwargs):
            started.set()
            while True:
                events.check_cancelled()
                time.sleep(.01)
        with self.mocks(), patch.object(youtube, "download_video", side_effect=download):
            self.create()
            self.assertTrue(started.wait(2))
            self.service.cancel("abcdefghijk")
            self.wait_idle()
        self.assertEqual(self.service.task("abcdefghijk").status, "cancelled")
        with self.mocks():
            self.service.retry("abcdefghijk", "retry-cancelled")
            self.wait_idle()
        self.assertEqual(self.service.task("abcdefghijk").status, "ready")

    def test_invalid_media_retries_five_times_and_never_uploads(self):
        with self.mocks(), patch.object(media, "prepare_upload_video", side_effect=InvalidMediaError("broken")) as check:
            self.create()
            self.wait_idle()
            self.assertEqual(check.call_count, 5)
            self.assertEqual(self.service.task("abcdefghijk").status, "failed")
            self.assertFalse(self.uploads)

    def test_repair_preserves_bv_without_posting(self):
        task = Task("abcdefghijk", "https://youtu.be/abcdefghijk", "submitted", bv_id="BV1234567890")
        task.account_id = self.account["account_id"]
        task.account_uid_snapshot = self.account["uid"]
        self.service.store.upsert(task)
        with self.mocks():
            self.service.repair(task.video_id, "repair-operation")
            self.wait_idle()
        task = self.service.task(task.video_id)
        self.assertEqual((task.status, task.bv_id), ("submitted", "BV1234567890"))
        self.assertTrue(Path(task.video_path).is_file())
        self.assertFalse(self.uploads)

    def test_settings_and_key_never_leak_secret_to_disk_or_response(self):
        result = self.service.config.public()
        self.assertNotIn("test-key", json.dumps(result))
        self.service.update_settings({"theme": "dark"})
        self.assertNotIn("test-key", (self.paths.root / "settings.json").read_text(encoding="utf-8"))
        with self.assertRaises(Yt2BiliError): self.service.update_settings({"upload_gap_seconds": -1})
        with self.assertRaises(Yt2BiliError): self.service.update_settings({"deepl_auth_key": "bad"})

    def test_local_task_creation_does_not_require_deepl_vault(self):
        self.service.config.set_key("")
        with self.mocks(), patch.object(self.service.config, "key", side_effect=AssertionError("must not read key")):
            self.create()
            self.wait_idle()
        self.assertEqual(self.service.task("abcdefghijk").status, "ready")

    def test_retranslation_requires_confirmation_preserves_on_error_and_never_uploads(self):
        from yt2bili.translation.types import TranslationError, TranslationResult
        with self.mocks():
            self.create(); self.wait_idle()
        self.service.update_metadata("abcdefghijk", "用户编辑", "用户正文")
        with self.assertRaises(Yt2BiliError):
            self.service.retranslate("abcdefghijk", "retranslate-no-confirm")
        with patch("yt2bili.translation.tasks.translate_group", side_effect=TranslationError("FAIL", "模拟翻译失败")):
            self.service.retranslate("abcdefghijk", "retranslate-fail", replace_edited=True)
            self.wait_idle()
        self.assertEqual(self.service.task("abcdefghijk").title_zh, "用户编辑")
        self.assertEqual(self.service.task("abcdefghijk").status, "ready")
        with patch("yt2bili.translation.tasks.translate_group", return_value=TranslationResult("新翻译", "新正文", "local_llm")):
            self.service.retranslate("abcdefghijk", "retranslate-success", replace_edited=True)
            self.wait_idle()
        self.assertEqual(self.service.task("abcdefghijk").title_zh, "新翻译")
        self.assertEqual(self.service.task("abcdefghijk").status, "ready")
        self.assertFalse(self.uploads)

    def test_translation_job_returns_immediately_and_is_idempotent(self):
        from yt2bili.translation.types import TranslationResult
        started, release=threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            started.set(); release.wait(3)
            return TranslationResult("试译", "正文", "local_llm")
        with patch("yt2bili.translation.service.translate", side_effect=slow):
            first=self.service.translation_test("local_llm", "same-operation")
            self.assertTrue(started.wait(1))
            second=self.service.translation_test("local_llm", "same-operation")
            self.assertEqual(first,second)
            self.service.translation_jobs.cancel(first["job_id"])
            release.set()
            until=time.monotonic()+3
            while self.service.translation_jobs.active() and time.monotonic()<until:time.sleep(.01)
            self.assertEqual(self.service.translation_jobs.get(first["job_id"])["state"],"cancelled")

    def test_configuration_and_edit_are_blocked_for_active_task(self):
        started, release = threading.Event(), threading.Event()
        def download(*args, **kwargs):
            started.set(); release.wait(3); return self.download(*args, **kwargs)
        with self.mocks(), patch.object(youtube, "download_video", side_effect=download):
            try:
                self.create(); self.assertTrue(started.wait(2))
                with self.assertRaises(Yt2BiliError): self.service.update_settings({"bili_tid": 12})
                with self.assertRaises(Yt2BiliError): self.service.update_metadata("abcdefghijk", "new", "desc")
            finally:
                release.set()
            self.wait_idle()

    def test_unknown_method_and_bad_parameters_fail_closed(self):
        with self.assertRaises(Yt2BiliError): self.service.dispatch("shell.execute", {})
        with self.assertRaises(Yt2BiliError): self.service.dispatch("tasks.list", [])
        with self.assertRaises(Yt2BiliError): self.service.list_tasks(limit=100000)


class ContractTests(unittest.TestCase):
    def test_parse_urls_validates_host_and_deduplicates_video_id(self):
        items = parse_urls("https://www.youtube.com/watch?v=-abcdefghij&t=10")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][0], "-abcdefghij")
        for invalid in ("file:///tmp/abcdefghijk", "https://youtube.com.evil.test/watch?v=abcdefghijk", "https://youtube.com/playlist?list=123"):
            with self.assertRaises(Yt2BiliError): parse_urls(invalid)

    def test_protocol_success_error_and_invalid_version(self):
        class Service:
            def dispatch(self, method, params):
                if method == "fail": raise Yt2BiliError("safe message")
                return {"ok": True}
        stream = io.StringIO()
        protocol = Protocol(stream)
        for method in ("ok", "fail"):
            protocol.handle(Service(), {"protocol_version": 2, "request_id": method, "method": method})
        protocol.handle(Service(), {"protocol_version": 1, "request_id": "old"})
        lines = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertTrue(lines[0]["result"]["ok"])
        self.assertEqual(lines[1]["error"]["message"], "safe message")
        self.assertIn("error", lines[2])

    def test_protocol_concurrent_writes_remain_json_lines(self):
        from concurrent.futures import ThreadPoolExecutor
        stream = io.StringIO(); protocol = Protocol(stream)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n: protocol.emit("progress", {"n": n, "text": "中文\n分行"}), range(100)))
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(len(rows), 100)
        self.assertEqual(len({r["event_id"] for r in rows}), 100)

    def test_migration_backs_up_legacy_and_rejects_future(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tasks.sqlite"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE original (value TEXT)"); conn.commit()
            store = TaskStore(path); store.close()
            self.assertTrue(Path(str(path) + ".pre-desktop.bak").is_file())
            with closing(sqlite3.connect(path)) as conn: conn.execute("PRAGMA user_version=99")
            with self.assertRaises(Yt2BiliError): TaskStore(path)

    def test_startup_recovers_states_without_restarting_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = AppPaths.default(folder, folder)
            store = TaskStore(paths.root / "data/tasks.sqlite")
            store.upsert(Task("abcdefghijk", "url", "uploading"))
            store.upsert(Task("12345678901", "url", "downloading"))
            store.close()
            service = DesktopService(paths, lambda *args: None, MemoryVault())
            try:
                self.assertEqual(service.task("abcdefghijk").status, "submission_unknown")
                self.assertEqual(service.task("12345678901").status, "interrupted")
                self.assertFalse(service.scheduler.snapshot()["active"])
            finally: service.close()

    def test_file_lock_rejects_second_owner_and_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "task.lock"
            with FileLock(path):
                with self.assertRaises(Yt2BiliError):
                    with FileLock(path): pass
            with FileLock(path): pass

    def test_cancelled_qr_response_cannot_replace_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "cookie.json"
            session = LoginSession(destination, lambda *args: None)
            session.cancel()
            session.call = lambda *args: {"code": 0, "data": {"url": "https://example.com", "auth_code": "fixture"}}
            session._run()
            self.assertFalse(destination.exists())

    def test_qr_success_stores_biliup_schema_without_emitting_tokens(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "cookie.json"
            emitted = []
            session = LoginSession(destination, lambda *args: emitted.append(args))
            responses = [{"code": 0, "data": {"url": "https://example.com", "auth_code": "private-code"}},
                         {"code": 0, "data": login_fixture()}]
            with patch.object(session, "call", side_effect=responses), patch.object(session.cancelled, "wait", return_value=False):
                session._run()
            info = validate_login(json.loads(destination.read_text(encoding="utf-8")))
            self.assertEqual(info["platform"], "BiliTV")
            self.assertEqual(emitted[-1][1]["status"], "success")
            self.assertNotIn("private-code", json.dumps(emitted))
            self.assertNotIn("access_token", json.dumps(emitted))

    def test_expired_qr_does_not_store_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "cookie.json"
            emitted = []
            session = LoginSession(destination, lambda *args: emitted.append(args))
            responses = [{"code": 0, "data": {"url": "https://example.com", "auth_code": "fixture"}}, {"code": 86038}]
            with patch.object(session, "call", side_effect=responses), patch.object(session.cancelled, "wait", return_value=False):
                session._run()
            self.assertFalse(destination.exists())
            self.assertEqual(emitted[-1][1]["status"], "expired")

    def test_credentials_validation_and_log_redaction(self):
        self.assertEqual(validate_login(login_fixture())["token_info"]["mid"], 123)
        with self.assertRaises(Yt2BiliError): validate_login({"cookie_info": {}})
        self.assertNotIn("secret", redact('SESSDATA=secret'))
        self.assertNotIn("secret", redact('{"access_token":"secret"}'))


if __name__ == "__main__": unittest.main()

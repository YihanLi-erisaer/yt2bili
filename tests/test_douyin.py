import json
import sqlite3
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import test_desktop as desktop_tests
from test_multi_account import credentials
from yt2bili import bili_upload, publications
from yt2bili.db import TaskStore
from yt2bili.douyin import BrokerError
from yt2bili.exceptions import Yt2BiliError


class DouyinTests(unittest.TestCase):
    setUp = desktop_tests.DesktopTests.setUp
    wait_until = desktop_tests.DesktopTests.wait_until
    wait_idle = desktop_tests.DesktopTests.wait_idle
    mocks = desktop_tests.DesktopTests.mocks
    meta = desktop_tests.DesktopTests.meta
    download = desktop_tests.DesktopTests.download
    prepare = desktop_tests.DesktopTests.prepare
    upload = desktop_tests.DesktopTests.upload

    def broker(self, method, path, data=None, file=None):
        if path == "/v1/account":
            return {"account": {"client_key": "test-app", "open_id": "test-open-id", "nickname": "抖音测试账号"}, "capabilities": {"auto_publish": True}}
        if method == "PUT":
            self.assertTrue(Path(file).is_file())
            return {"uploaded": True}
        if path.endswith("/submit"):
            return {"status": "submitted", "remote_id": "douyin-item-1"}
        return {"status": "ready"}

    def create_dual(self, video="abcdefghijk", mode="preview", account=None):
        return self.service.create("https://youtu.be/" + video, str(uuid.uuid4()), account or self.account["account_id"], mode, sync_douyin=True)["task_id"]

    def test_not_logged_in_cannot_create_sync_task(self):
        with patch.object(self.service.douyin, "request", return_value={"account": None}):
            with self.assertRaises(Yt2BiliError): self.create_dual()
        self.assertEqual(self.service.store.list_all(), [])

    def test_auto_requires_approved_capability(self):
        with patch.object(self.service.douyin, "request", return_value={"account": {"client_key": "a", "open_id": "u"}, "capabilities": {"auto_publish": False}}):
            with self.assertRaises(Yt2BiliError): self.create_dual(mode="auto")
        self.assertEqual(self.service.store.list_all(), [])

    def test_preview_confirms_both_once_and_uses_independent_text(self):
        with self.mocks(), patch.object(self.service.douyin, "request", side_effect=self.broker) as api:
            identity = self.create_dual()
            self.wait_idle()
            self.assertEqual(len(self.uploads), 0)
            pubs = publications.items(self.service.store, identity)
            self.assertEqual([p["status"] for p in pubs], ["ready", "ready"])
            dy = pubs[1]
            self.service.update_publication(dy["publication_id"], "仅抖音使用的文案", dy["revision"])
            self.service.submit(identity, "submit-dual")
            self.service.submit(identity, "submit-dual")
            self.wait_idle()
            self.assertEqual(self.service.task(identity).status, "submitted")
            self.assertEqual(len(self.uploads), 1)
            registration = [c for c in api.call_args_list if c.args[0] == "POST" and c.args[1].startswith("/v1/publications/") and not c.args[1].endswith("/submit")]
            self.assertEqual(registration[0].args[2]["text"], "仅抖音使用的文案")
            self.assertEqual(self.uploads[0][1], "翻译标题")
            self.assertEqual(self.service.task(identity).cleanup_state, "done")

    def test_concurrent_branches_keep_assets_until_both_finish(self):
        dy_started, bili_started, release = threading.Event(), threading.Event(), threading.Event()
        def api(method, path, data=None, file=None):
            if path.endswith("/submit"):
                dy_started.set()
                release.wait(10)
            return self.broker(method, path, data, file)
        def upload(*args, **kwargs):
            bili_started.set()
            self.assertTrue(dy_started.wait(10))
            return self.upload(*args, **kwargs)
        try:
            with self.mocks(), patch.object(self.service.douyin, "request", side_effect=api), patch.object(bili_upload, "upload", side_effect=upload):
                identity = self.create_dual(mode="auto")
                self.wait_until(lambda: bili_started.is_set() and dy_started.is_set(), "independent uploads")
                self.wait_until(lambda: bool(self.service.task(identity).bv_id), "Bili receipt")
                self.assertTrue(Path(self.service.task(identity).video_path).is_file())
                self.assertTrue(self.service.scheduler.snapshot()["active"])
                release.set()
                self.wait_idle()
                self.assertEqual(self.service.task(identity).cleanup_state, "done")
        finally: release.set()

    def test_failure_retry_does_not_repeat_successful_bili(self):
        def failure(method, path, data=None, file=None):
            if path.endswith("/submit"): return {"status": "failed", "error": "platform rejected"}
            return self.broker(method, path, data, file)
        with self.mocks(), patch.object(self.service.douyin, "request", side_effect=failure):
            identity = self.create_dual(mode="auto")
            self.wait_idle()
            self.assertEqual(self.service.task(identity).status, "partial_success")
            self.assertTrue(Path(self.service.task(identity).video_path).is_file())
            p = publications.for_platform(self.service.store, identity, "douyin")
            self.service.archive_account(self.account["account_id"])
            self.service.retry_publication(p["publication_id"], "retry-dy-only")
            self.assertEqual(len(self.uploads), 1)
        with self.mocks(), patch.object(self.service.douyin, "request", side_effect=self.broker):
            self.service.submit(identity, "submit-dy-only")
            self.wait_idle()
            self.assertEqual(len(self.uploads), 1)
            self.assertEqual(self.service.task(identity).status, "submitted")

    def test_unknown_is_not_retried_or_cleaned(self):
        calls = []
        def failure(method, path, data=None, file=None):
            calls.append((method, path))
            if path.endswith("/submit") or method == "GET" and path.startswith("/v1/publications/"):
                raise BrokerError("response lost")
            return self.broker(method, path, data, file)
        with self.mocks(), patch.object(self.service.douyin, "request", side_effect=failure):
            identity = self.create_dual(mode="auto")
            self.wait_idle()
            self.assertEqual(self.service.task(identity).status, "submission_unknown")
            p = publications.for_platform(self.service.store, identity, "douyin")
            with self.assertRaises(Yt2BiliError): self.service.retry_publication(p["publication_id"], "no-unknown-retry")
            with self.assertRaises(Yt2BiliError): self.service.abandon_publication(p["publication_id"])
            self.assertTrue(Path(self.service.task(identity).video_path).is_file())
            self.assertEqual(sum(path.endswith("/submit") for _, path in calls), 1)

    def test_duplicate_across_bili_accounts_is_atomic(self):
        second = self.service.accounts.bind(credentials(555), verify=False)
        with self.mocks(), patch.object(self.service.douyin, "request", side_effect=self.broker):
            self.create_dual()
            self.wait_idle()
            with self.assertRaises(Yt2BiliError): self.create_dual(account=second["account_id"])
            self.assertEqual(len(self.service.store.list_all()), 1)
            self.service.create("https://youtu.be/abcdefghijk", "bili-only-second", second["account_id"])
            self.wait_idle()
            self.assertEqual(len(self.service.store.list_all()), 2)

    def test_identity_cannot_change_until_pending_targets_abandoned(self):
        with self.mocks(), patch.object(self.service.douyin, "request", side_effect=self.broker):
            identity = self.create_dual()
            self.wait_idle()
            with self.assertRaises(Yt2BiliError): self.service.douyin.archive()
            original = self.service.douyin.account()
            with self.assertRaises(sqlite3.IntegrityError), self.service.store.transaction() as db:
                db.execute("UPDATE douyin_accounts SET open_id='other'")
            p = publications.for_platform(self.service.store, identity, "douyin")
            self.service.abandon_publication(p["publication_id"])
            self.service.douyin.archive()
            self.assertIsNone(self.service.douyin.account())
            self.assertEqual(publications.get(self.service.store, p["publication_id"])["account_id"], original["account_id"])

    def test_stale_confirmation_does_not_enqueue_either_target(self):
        with self.mocks(), patch.object(self.service.douyin, "request", side_effect=self.broker):
            identity = self.create_dual()
            self.wait_idle()
            pubs = publications.items(self.service.store, identity)
            revisions = {p["publication_id"]: p["revision"] for p in pubs}
            self.service.update_publication(pubs[1]["publication_id"], "new text", pubs[1]["revision"])
            with self.assertRaises(Yt2BiliError): self.service.submit(identity, "stale-submit", list(revisions), revisions)
            self.assertEqual(len(self.uploads), 0)

    def test_douyin_fifo_does_not_block_next_shared_preparation(self):
        entered, release = threading.Event(), threading.Event()
        submissions = []
        def api(method, path, data=None, file=None):
            if path.endswith("/submit"):
                submissions.append(path.split("/")[-2])
                if len(submissions) == 1:
                    entered.set()
                    release.wait(10)
            return self.broker(method, path, data, file)
        try:
            with self.mocks(), patch.object(self.service.douyin, "request", side_effect=api):
                first = self.create_dual(mode="auto")
                self.wait_until(entered.is_set, "first Douyin upload")
                second = self.create_dual("bcdefghijkl", mode="auto")
                third = self.create_dual("cdefghijklm", mode="preview")
                self.wait_until(lambda: self.service.task(third).status == "ready", "preview during Douyin upload")
                self.wait_until(lambda: bool(self.service.task(second).bv_id), "second Bili upload")
                self.assertEqual(len(submissions), 1)
                release.set()
                self.wait_idle()
                expected = [publications.for_platform(self.service.store, t, "douyin")["publication_id"] for t in (first, second)]
                self.assertEqual(submissions, expected)
        finally: release.set()

    def test_rate_limit_pauses_only_douyin_and_requires_explicit_resume(self):
        limited = True
        def api(method, path, data=None, file=None):
            if path.endswith("/submit") and limited:
                return {"status": "failed", "error": "RATE_LIMITED: quota"}
            return self.broker(method, path, data, file)
        with self.mocks(), patch.object(self.service.douyin, "request", side_effect=api):
            identity = self.create_dual(mode="auto")
            self.wait_until(lambda: self.service.scheduler.snapshot()["douyin"]["wait_reason"] == "rate_limited", "Douyin pause")
            self.wait_until(lambda: bool(self.service.task(identity).bv_id), "Bili succeeds independently")
            self.assertEqual(self.service.task(identity).status, "partial_success")
            limited = False
            self.service.douyin.status(True)
            self.assertEqual(self.service.scheduler.snapshot()["douyin"]["wait_reason"], "rate_limited")
            self.service.douyin.resume()
            self.wait_idle()
            self.assertEqual(self.service.task(identity).status, "submitted")

    def test_shutdown_waiting_target_preserves_success_and_material(self):
        from yt2bili.douyin import AuthRequired
        with self.mocks(), patch.object(self.service.douyin, "request", side_effect=self.broker), patch.object(self.service.douyin, "publish", side_effect=AuthRequired("expired")):
            identity = self.create_dual(mode="auto")
            self.wait_until(lambda: bool(self.service.task(identity).bv_id), "Bili receipt")
            self.service.scheduler.prepare_shutdown()
            self.wait_idle()
            pubs = publications.items(self.service.store, identity)
            self.assertEqual(pubs[0]["status"], "submitted")
            self.assertEqual(pubs[1]["status"], "cancelled")
            self.assertTrue(Path(self.service.task(identity).video_path).is_file())

    def test_douyin_validation_failure_does_not_block_bili(self):
        with self.mocks(), patch.object(self.service.douyin, "request", side_effect=self.broker), patch.object(self.service.douyin, "validate_assets", side_effect=Yt2BiliError("视频超过 15 分钟")):
            identity = self.create_dual(mode="auto")
            self.wait_idle()
            pubs = publications.items(self.service.store, identity)
            self.assertEqual(pubs[0]["status"], "submitted")
            self.assertEqual(pubs[1]["status"], "blocked_validation")
            self.assertTrue(Path(self.service.task(identity).video_path).is_file())

    def test_v3_migration_backs_up_and_does_not_create_douyin_targets(self):
        path = Path(self.tmp.name) / "migration" / "tasks.sqlite"
        from yt2bili.db import Task
        store = TaskStore(path)
        task = Task("abcdefghijk", "url", "submitted", bv_id="BV1234567890")
        store.upsert(task)
        store._conn.execute("DROP TABLE task_publications")
        store._conn.execute("DROP TABLE douyin_accounts")
        store._conn.execute("PRAGMA user_version=3")
        store.close()
        migrated = TaskStore(path)
        try:
            self.assertTrue(Path(str(path) + ".pre-v4.bak").is_file())
            pubs = publications.items(migrated, task.task_id)
            self.assertEqual(len(pubs), 1)
            self.assertEqual((pubs[0]["platform"], pubs[0]["remote_id"]), ("bilibili", task.bv_id))
            self.assertEqual(migrated._conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally: migrated.close()


class BrokerTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from cryptography.fernet import Fernet
        from yt2bili.douyin_broker import Broker
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        class FakeAPI:
            def call(inner, path, **kwargs):
                self.calls.append(path)
                if "access_token" in path:
                    return {"access_token": "secret-access", "refresh_token": "secret-refresh", "open_id": "o", "scope": "video.create.bind", "expires_in": 3600}
                return {"item_id": "item-receipt"}
            def upload(inner, *args): return "media-id"
        self.key = Fernet.generate_key()
        self.broker = Broker(self.tmp.name, "app", "secret", "https://test.example/oauth/callback", "x"*32, self.key, api=FakeAPI())
        self.addCleanup(self.broker.db.close)
        self.broker.auth_start()
        state = json.loads(self.broker.value("oauth"))["state"]
        self.broker.callback_code("code", state)
        self.identity = str(uuid.uuid4())
        self.payload = {"client_key": "app", "open_id": "o", "source_video_id": "abcdefghijk", "text": "文案", "auto": False}

    def registered(self):
        self.broker.register(self.identity, self.payload)
        self.broker.update(self.identity, media_id="video", cover_id="cover")

    def test_oauth_state_one_use_and_tokens_encrypted(self):
        from yt2bili.douyin_broker import Rejected
        with self.assertRaises(Rejected): self.broker.callback_code("code", "wrong-state")
        self.assertNotIn("secret-access", self.broker.value("token"))
        self.assertEqual(self.broker.token()["open_id"], "o")

    def test_receipt_idempotency_and_unique_source(self):
        self.registered()
        self.assertEqual(self.broker.submit(self.identity), self.broker.submit(self.identity))
        self.assertEqual(self.calls.count("/api/douyin/v1/video/create_video/"), 1)
        with self.assertRaises(ValueError): self.broker.register(str(uuid.uuid4()), self.payload)

    def test_timeout_stays_unknown_without_repeat_post(self):
        self.registered()
        with patch.object(self.broker.api, "call", side_effect=OSError("lost response")) as api:
            self.assertEqual(self.broker.submit(self.identity)["status"], "submission_unknown")
            self.assertEqual(self.broker.submit(self.identity)["status"], "submission_unknown")
            self.assertEqual(api.call_count, 1)

    def test_restart_creating_is_unknown(self):
        from yt2bili.douyin_broker import Broker
        self.registered()
        self.broker.update(self.identity, status="creating")
        reopened = Broker(self.tmp.name, "app", "secret", "https://test.example/oauth/callback", "x"*32, self.key)
        try: self.assertEqual(reopened.publication(self.identity)["status"], "submission_unknown")
        finally: reopened.db.close()

    def test_explicit_business_error_is_failed_not_success(self):
        from yt2bili.douyin_broker import Rejected
        self.registered()
        with patch.object(self.broker.api, "call", side_effect=Rejected("error_code=28001018", code="28001018")):
            self.assertEqual(self.broker.submit(self.identity)["status"], "failed")

    def test_platform_internal_network_error_is_uncertain(self):
        from yt2bili.douyin_broker import Rejected
        self.registered()
        with patch.object(self.broker.api, "call", side_effect=Rejected("internal network error", code="28001006")):
            self.assertEqual(self.broker.submit(self.identity)["status"], "submission_unknown")

    def test_unknown_resolution_is_explicit_and_editable_only_after_resolution(self):
        self.registered()
        self.broker.update(self.identity, status="submission_unknown")
        with self.assertRaises(ValueError): self.broker.register(self.identity, {**self.payload, "text": "other"})
        result = self.broker.route("POST", "/v1/publications/" + self.identity + "/resolve", {"not_submitted": True})
        self.assertEqual(result["status"], "ready")
        self.broker.register(self.identity, {**self.payload, "text": "other"})
        self.assertEqual(self.broker.publication(self.identity)["media_id"], "")

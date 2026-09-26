import json
import sqlite3
from contextlib import closing
import uuid
import unittest
from pathlib import Path
from unittest.mock import patch

import test_desktop as desktop_tests
from test_multi_account import credentials
from yt2bili import publications
from yt2bili.acfun import UnknownResult, WebClient
from yt2bili.db import TaskStore
from yt2bili.db import Task
from yt2bili.desktop_service import DesktopService
from yt2bili.exceptions import Yt2BiliError


class FakeWeb:
    def __init__(self, fail=""):
        self.calls = []
        self.fail = fail

    def request(self, method, url, data=None, raw=None, code=0):
        stage = url.split("?", 1)[0].split("/")[-1]
        self.calls.append(stage)
        if stage == self.fail:
            if stage in ("createVideo", "createDouga"):
                raise UnknownResult("response lost")
            raise Yt2BiliError("upstream failure")
        return {
            "getKSCloudToken": {"result": 0, "taskId": 123, "token": "video-secret", "uploadConfig": {"partSize": 5}},
            "fragment": {"result": 1}, "complete": {"result": 1},
            "createVideo": {"result": 0, "videoId": 456},
            "getQiniuToken": {"result": 0, "info": {"token": "cover-secret"}},
            "getUrlAfterUpload": {"result": 0, "url": "https://member.acfun.cn/cover.jpg"},
            "createDouga": {"result": 0, "dougaId": 789},
        }.get(stage, {"result": 0})


class AcfunTests(unittest.TestCase):
    setUp = desktop_tests.DesktopTests.setUp
    wait_idle = desktop_tests.DesktopTests.wait_idle
    wait_until = desktop_tests.DesktopTests.wait_until
    mocks = desktop_tests.DesktopTests.mocks
    meta = desktop_tests.DesktopTests.meta
    download = desktop_tests.DesktopTests.download
    prepare = desktop_tests.DesktopTests.prepare
    upload = desktop_tests.DesktopTests.upload

    def bind(self):
        self.service.config.values["acfun_experimental_enabled"] = True
        with self.service.store.transaction() as db:
            db.execute("INSERT INTO acfun_accounts(account_id,user_id,nickname) VALUES('ac-test','12345','测试 AcFun')")
        return self.service.acfun.account()

    def test_qr_login_binds_verified_id_and_keeps_cookies_in_vault(self):
        class QrClient:
            def request(self, method, url, data=None, raw=None, code=0):
                if "/qr/start" in url:
                    return {"result": 0, "qrLoginToken": "qr-token", "qrLoginSignature": "signature", "imageData": "aW1hZ2U="}
                if "/qr/scanResult" in url: return {"result": 0, "qrLoginSignature": "next"}
                if "/qr/acceptResult" in url: return {"result": 0}
                if "/personalInfo" in url: return {"result": 0, "info": {"userId": 12345, "name": "AcFun 测试"}}
                raise AssertionError(url)
            def cookies(self): return [{"name": "auth_key", "value": "secret", "domain": ".acfun.cn", "path": "/"}]
        self.service.acfun.client_factory = lambda cookies=None: QrClient()
        self.assertEqual(self.service.acfun.start()["status"], "waiting")
        self.assertEqual(self.service.acfun.poll()["status"], "scanned")
        self.assertEqual(self.service.acfun.poll()["status"], "done")
        self.assertEqual(self.service.acfun.account()["user_id"], "12345")
        self.assertEqual(self.service.acfun._credential()[0]["value"], "secret")
        self.assertNotIn("secret", str(self.service.acfun.account()))

    def create(self, video="abcdefghijk"):
        return self.service.create("https://youtu.be/" + video, str(uuid.uuid4()), self.account["account_id"], sync_acfun=True)["task_id"]

    def ready(self, task_id):
        pub = publications.for_platform(self.service.store, task_id, "acfun")
        self.service.update_publication(pub["publication_id"], revision=pub["revision"], title="转载测试", description="已获授权", channel_id=90, tags=["转载"])
        return publications.for_platform(self.service.store, task_id, "acfun")

    def test_preview_and_submit_only_once(self):
        self.bind()
        fake = FakeWeb()
        with self.mocks(), patch.object(self.service.acfun, "check", return_value=self.service.acfun.account()), patch.object(self.service.acfun, "_client", return_value=fake):
            task_id = self.create()
            self.wait_idle()
            self.assertEqual(len(self.uploads), 0)
            pub = self.ready(task_id)
            self.service.submit(task_id, "submit-acfun", revisions={p["publication_id"]: p["revision"] for p in publications.items(self.service.store, task_id)})
            self.wait_idle()
        pub = publications.for_platform(self.service.store, task_id, "acfun")
        self.assertEqual((pub["status"], pub["remote_id"]), ("submitted", "AC789"))
        self.assertEqual(fake.calls.count("createDouga"), 1)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.service.task(task_id).status, "submitted")
        attempt = self.service.store._conn.execute("SELECT media_sha256,request_sha256 FROM acfun_attempts").fetchone()
        self.assertEqual((len(attempt[0]), len(attempt[1])), (64, 64))

    def test_fragment_failure_never_creates_work(self):
        self.bind()
        fake = FakeWeb("fragment")
        with self.mocks(), patch.object(self.service.acfun, "check", return_value=self.service.acfun.account()), patch.object(self.service.acfun, "_client", return_value=fake):
            task_id = self.create()
            self.wait_idle()
            self.ready(task_id)
            self.service.submit(task_id, "submit-failure")
            self.wait_idle()
        self.assertNotIn("createDouga", fake.calls)
        self.assertEqual(publications.for_platform(self.service.store, task_id, "acfun")["status"], "failed")

    def test_three_targets_share_preparation_and_finish_independently(self):
        self.bind()
        fake = FakeWeb()
        def broker(method, path, data=None, file=None):
            if path == "/v1/account":
                return {"account": {"client_key": "client", "open_id": "dy-user", "nickname": "抖音"}, "capabilities": {"auto_publish": False}}
            if path.endswith("/submit"): return {"status": "submitted", "remote_id": "dy-receipt"}
            return {"status": "ready"}
        with self.mocks(), patch.object(self.service.douyin, "request", side_effect=broker), patch.object(self.service.acfun, "check", return_value=self.service.acfun.account()), patch.object(self.service.acfun, "_client", return_value=fake):
            task_id = self.service.create("https://youtu.be/abcdefghijk", str(uuid.uuid4()), self.account["account_id"], sync_douyin=True, sync_acfun=True)["task_id"]
            self.wait_idle()
            self.ready(task_id)
            self.service.submit(task_id, "submit-three")
            self.wait_idle()
        pubs = publications.items(self.service.store, task_id)
        self.assertEqual({p["platform"]: p["status"] for p in pubs}, {"bilibili": "submitted", "douyin": "submitted", "acfun": "submitted"})
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(fake.calls.count("createDouga"), 1)

    def test_lost_create_response_requires_manual_resolution(self):
        self.bind()
        fake = FakeWeb("createDouga")
        with self.mocks(), patch.object(self.service.acfun, "check", return_value=self.service.acfun.account()), patch.object(self.service.acfun, "_client", return_value=fake):
            task_id = self.create()
            self.wait_idle()
            self.ready(task_id)
            self.service.submit(task_id, "submit-unknown")
            self.wait_idle()
        pub = publications.for_platform(self.service.store, task_id, "acfun")
        self.assertEqual(pub["status"], "submission_unknown")
        self.assertEqual(pub["retain_assets"], 1)
        self.assertEqual(fake.calls.count("createDouga"), 1)
        with self.assertRaises(Yt2BiliError): self.service.retry_publication(pub["publication_id"], "retry-unknown")
        self.service.resolve_publication(pub["publication_id"], not_submitted=True)
        self.assertTrue(self.service.retry_publication(pub["publication_id"], "retry-after-check")["ready"])

    def test_same_source_acfun_account_is_unique(self):
        self.bind()
        with patch.object(self.service.acfun, "check", return_value=self.service.acfun.account()):
            self.create()
            other = self.service.accounts.bind(credentials(456), verify=False)
            # A second Bilibili account is distinct but the AcFun target is not.
            with self.assertRaises(Yt2BiliError):
                self.service.create("https://youtu.be/abcdefghijk", str(uuid.uuid4()), other["account_id"], sync_acfun=True)

    def test_reject_html_and_unapproved_cookie_domain(self):
        self.assertEqual(WebClient([{"name": "secret", "value": "x", "domain": ".evil.com"}]).cookies(), [])
        with self.assertRaises(Yt2BiliError):
            WebClient().request("GET", "https://evil.example/test")
        class Response:
            status = 200
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, *_): return self.body
        client = WebClient()
        with patch.object(client.opener, "open", return_value=Response(b"<html>login</html>")):
            with self.assertRaises(UnknownResult): client.request("POST", "https://member.acfun.cn/video/api/createDouga", {})
        with patch.object(client.opener, "open", return_value=Response(b'{"result":1}')):
            with self.assertRaises(Yt2BiliError): client.request("POST", "https://member.acfun.cn/video/api/createDouga", {})

    def test_v4_upgrade_preserves_douyin_receipt_and_creates_backup(self):
        path = Path(self.tmp.name) / "legacy-v4.sqlite"
        old = TaskStore(path)
        task = Task("abcdefghijk", "https://youtu.be/abcdefghijk", "submitted", task_id=str(uuid.uuid4()))
        old.upsert(task)
        publications.ensure_bili(old, task)
        with old.transaction() as db:
            db.execute("INSERT INTO douyin_accounts(account_id,client_key,open_id,nickname) VALUES('dy','client','user','抖音')")
            db.execute("INSERT INTO task_publications(publication_id,task_id,platform,account_id,source_video_id,status,remote_id) VALUES(?,?,'douyin','dy',?,'submitted','dy-receipt')", (str(uuid.uuid4()), task.task_id, task.video_id))
        rows = [tuple(r) for r in old._conn.execute("SELECT * FROM task_publications")]
        old.close()
        with closing(sqlite3.connect(path)) as db:
            db.execute("DROP TABLE acfun_attempts")
            db.execute("DROP TABLE acfun_accounts")
            db.execute("DROP TABLE task_publications")
            db.execute("""CREATE TABLE task_publications(
                publication_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                platform TEXT NOT NULL CHECK(platform IN ('bilibili','douyin')), account_id TEXT,
                source_video_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending_assets',
                revision INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL DEFAULT '',
                remote_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                retain_assets INTEGER NOT NULL DEFAULT 0, snapshot TEXT NOT NULL DEFAULT '{}',
                UNIQUE(task_id,platform))""")
            db.executemany("INSERT INTO task_publications VALUES(" + ",".join("?" for _ in range(12)) + ")", rows)
            db.execute("PRAGMA user_version=4")
            db.commit()
        migrated = TaskStore(path)
        try:
            self.assertEqual(migrated._conn.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual([tuple(r) for r in migrated._conn.execute("SELECT * FROM task_publications")], rows)
            self.assertTrue(Path(str(path) + ".pre-v5.bak").is_file())
            self.assertEqual(migrated._conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally: migrated.close()

    def test_restart_during_video_creation_requires_review(self):
        self.bind()
        with self.mocks(), patch.object(self.service.acfun, "check", return_value=self.service.acfun.account()):
            task_id = self.create()
            self.wait_idle()
        pub = publications.for_platform(self.service.store, task_id, "acfun")
        with self.service.store.transaction() as db:
            publications.change(self.service.store, pub["publication_id"], status="uploading_media")
            db.execute("INSERT INTO acfun_attempts(attempt_id,publication_id,phase,started_at) VALUES(?,?,'creating_video','2026-09-26T00:00:00Z')",
                       (str(uuid.uuid4()), pub["publication_id"]))
        self.service.close()
        reopened = DesktopService(self.paths, lambda *_: None, desktop_tests.MemoryVault())
        self.addCleanup(reopened.close)
        state = publications.for_platform(reopened.store, task_id, "acfun")
        self.assertEqual(state["status"], "submission_unknown")
        self.assertEqual(reopened.task(task_id).status, "submission_unknown")

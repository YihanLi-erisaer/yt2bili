from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from yt2bili.translation.config import DEFAULTS
from yt2bili.translation.deployment import download_runtime
from yt2bili.translation.runtime import local_session, runtime_path, sha256
from yt2bili.translation.jobs import TranslationJobs
from yt2bili.translation.types import TranslationError


class Response(io.BytesIO):
    def __init__(self, data, status, content_range=""):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Range": content_range}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "downloads").mkdir()
        self.payload = b"verified archive data"
        self.spec = {"runtime": {"url": "https://github.com/ollama/ollama/releases/download/fixed/runtime.zip",
                                 "sha256": hashlib.sha256(self.payload).hexdigest(), "size": len(self.payload)}}

    def test_download_resumes_only_when_content_range_matches(self):
        (self.root / "downloads/runtime.part").write_bytes(self.payload[:4])
        response = Response(self.payload[4:], 206, f"bytes 4-{len(self.payload)-1}/{len(self.payload)}")
        with patch("yt2bili.translation.deployment.manifest", return_value=self.spec), patch("yt2bili.translation.deployment.urlopen", return_value=response) as request:
            path = download_runtime(self.root)
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertEqual(request.call_args.args[0].headers["Range"], "bytes=4-")

    def test_ignored_range_restarts_download_without_appending(self):
        (self.root / "downloads/runtime.part").write_bytes(b"stale")
        with patch("yt2bili.translation.deployment.manifest", return_value=self.spec), patch("yt2bili.translation.deployment.urlopen", return_value=Response(self.payload,200)):
            self.assertEqual(download_runtime(self.root).read_bytes(), self.payload)

    def test_invalid_checksum_is_not_installed(self):
        with patch("yt2bili.translation.deployment.manifest", return_value=self.spec), patch("yt2bili.translation.deployment.urlopen", return_value=Response(b"x"*len(self.payload),200)):
            with self.assertRaises(TranslationError) as raised:
                download_runtime(self.root)
        self.assertEqual(raised.exception.code,"CHECKSUM_FAILED")
        self.assertFalse((self.root/'downloads/runtime.zip').exists())
        self.assertFalse((self.root/'downloads/runtime.part').exists())

    def test_busy_port_does_not_take_ownership_or_kill_service(self):
        binary=runtime_path(self.root); binary.parent.mkdir(parents=True);binary.write_bytes(b"fixture")
        (binary.parent/'installed.json').write_text(json.dumps({'binary_sha256':sha256(binary)}))
        with patch('yt2bili.translation.runtime.sys.platform','win32'), patch('yt2bili.translation.runtime.platform.machine',return_value='AMD64'), patch('yt2bili.translation.runtime.request_json',return_value={'version':'other'}), patch('yt2bili.translation.runtime.subprocess.Popen') as launch:
            with self.assertRaises(TranslationError) as raised:
                with local_session(DEFAULTS,self.root):pass
        self.assertEqual(raised.exception.code,'PORT_IN_USE')
        launch.assert_not_called()

    def test_jobs_restore_as_interrupted_instead_of_success(self):
        (self.root/'jobs.json').write_text(json.dumps({'one':{'job_id':'one','operation_id':'operation','kind':'install','state':'running'}}))
        jobs=TranslationJobs(self.root,lambda *args:None)
        self.addCleanup(jobs.close)
        self.assertEqual(jobs.get('one')['state'],'interrupted')
        self.assertFalse(jobs.active())

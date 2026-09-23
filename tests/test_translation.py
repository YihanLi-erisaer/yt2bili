from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from yt2bili import events
from yt2bili.translation.config import DEFAULTS, validate, from_env
from yt2bili.translation.service import translate, isolated_request
from yt2bili.translation.types import TranslationError
from yt2bili.translation.runtime import manifest
from yt2bili.translation import deepl_provider


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = SimpleNamespace(**DEFAULTS, translation_root=Path(self.temp.name), deepl_auth_key="test-key")

    def translate(self, fn, **kwargs):
        return translate(self.settings, "Original", "Description", "en", 80, 1000, request_fn=fn, **kwargs)

    @staticmethod
    def successful(payload, deadline):
        return {"title": "标题", "description": "简介", "provider": payload["provider"]}

    def test_default_local_does_not_even_read_credentials(self):
        self.settings.deepl_key_provider = Mock(side_effect=AssertionError("must not read vault"))
        calls = Mock(side_effect=self.successful)
        result = self.translate(calls)
        self.assertEqual(result.provider, "local_llm")
        self.assertEqual(calls.call_count, 1)
        self.settings.deepl_key_provider.assert_not_called()

    def test_both_fallback_directions_are_bounded(self):
        for primary in ("local_llm", "deepl"):
            with self.subTest(primary=primary):
                self.settings.translation_primary = primary
                seen = []
                def request(payload, deadline):
                    seen.append(payload["provider"])
                    if payload["provider"] == primary:
                        raise TranslationError("UNAVAILABLE", "服务不可用")
                    return self.successful(payload, deadline)
                result = self.translate(request)
                self.assertEqual(seen, [primary, "deepl" if primary == "local_llm" else "local_llm"])
                self.assertTrue(result.fallback_used)
                self.assertEqual(result.fallback_reason, "UNAVAILABLE")

    def test_disabled_fallback_and_double_failure(self):
        for enabled, count in ((False, 1), (True, 2)):
            self.settings.translation_fallback_enabled = enabled
            fn = Mock(side_effect=TranslationError("UNAVAILABLE", "失败"))
            with self.assertRaises(TranslationError) as raised:
                self.translate(fn)
            self.assertEqual(fn.call_count, count)
            self.assertEqual(len(raised.exception.attempts), count)

    def test_deepl_success_does_not_call_local(self):
        self.settings.translation_primary = "deepl"
        fn = Mock(side_effect=self.successful)
        self.assertEqual(self.translate(fn).provider, "deepl")
        self.assertEqual(fn.call_count, 1)

    def test_missing_key_is_not_a_fake_success(self):
        self.settings.deepl_auth_key = ""
        self.settings.translation_primary = "deepl"
        result = self.translate(self.successful)
        self.assertEqual(result.provider, "local_llm")
        self.assertEqual(result.fallback_reason, "CREDENTIAL_MISSING")

    def test_chinese_and_empty_skip_all_services(self):
        fn = Mock(side_effect=AssertionError())
        for title, description, lang in (("原文", "简介", "zh-Hant"), ("", "", "en")):
            result = translate(self.settings, title, description, lang, 80, 1000, request_fn=fn)
            self.assertEqual(result.provider, "none")
        fn.assert_not_called()

    def test_retry_limited_to_two_and_pair_input_preserved(self):
        calls = []
        def request(payload, deadline):
            calls.append(payload)
            if payload["provider"] == "local_llm":
                raise TranslationError("OUTPUT_INVALID", "坏格式", retryable=True)
            return self.successful(payload, deadline)
        result = self.translate(request)
        self.assertEqual([p["provider"] for p in calls], ["local_llm", "local_llm", "deepl"])
        self.assertEqual(calls[0]["source"], calls[-1]["source"])
        self.assertEqual(result.title, "标题")

    def test_cancel_does_not_fallback_or_save_late_result(self):
        cancel = threading.Event()
        calls = Mock(side_effect=lambda payload, deadline: (cancel.set(), self.successful(payload, deadline))[1])
        with events.task_context("task", cancel, lambda *a: None):
            with self.assertRaises(events.Cancelled):
                self.translate(calls)
        self.assertEqual(calls.call_count, 1)

    def test_input_error_never_falls_back(self):
        fn = Mock(side_effect=TranslationError("INPUT_INVALID", "输入过长"))
        with self.assertRaises(TranslationError):
            self.translate(fn)
        self.assertEqual(fn.call_count, 1)

    def test_empty_output_rejected_and_title_clamped(self):
        fn = Mock(return_value={"title": "", "description": "", "provider": "local_llm"})
        with self.assertRaises(TranslationError):
            self.translate(fn)
        result = self.translate(lambda p, d: {"title": "字"*100, "description": "简介", "provider": p["provider"]})
        self.assertEqual(len(result.title), 80)

    def test_input_truncation_recorded(self):
        fn = Mock(side_effect=self.successful)
        result = translate(self.settings, "Title", "x"*2000, "en", 80, 200, request_fn=fn)
        self.assertTrue(result.input_truncated)
        self.assertEqual(len(fn.call_args.args[0]["source"]["description"]), 400)

    def test_invalid_addresses_and_booleans(self):
        for address in ("https://127.0.0.1:10", "http://localhost:11434", "http://127.0.0.1:2@evil.com:80", "http://10.0.0.1:11434", "http://127.0.0.1:11434/path"):
            with self.subTest(address=address), self.assertRaises(Exception):
                validate({"local_llm_mode": "external", "local_llm_base_url": address})
        with self.assertRaises(Exception):
            validate({"translation_fallback_enabled": "false"})
        with patch.dict("os.environ", {"TRANSLATION_FALLBACK_ENABLED": "false"}, clear=True):
            self.assertIs(from_env()["translation_fallback_enabled"], False)

    def test_deepl_whole_pair_and_exceptions(self):
        translator = Mock()
        translator.get_usage.return_value.any_limit_reached = False
        translator.translate_text.side_effect = [SimpleNamespace(text="标题", detected_source_lang="EN"), SimpleNamespace(text="简介", detected_source_lang="EN")]
        payload = {"key": "test", "source": {"title": "title", "description": "description", "source_lang": "en"}}
        with patch.object(deepl_provider.deepl, "Translator", return_value=translator):
            result = deepl_provider.translate(payload)
        self.assertEqual(result["title"], "标题")
        self.assertEqual(translator.translate_text.call_count, 2)
        translator.close.assert_called_once()
        import deepl
        for error, code in ((deepl.AuthorizationException("bad"), "AUTH_FAILED"),
                            (deepl.QuotaExceededException("quota"), "QUOTA_EXCEEDED"),
                            (deepl.TooManyRequestsException("rate"), "RATE_LIMITED")):
            with patch.object(deepl_provider.deepl, "Translator", side_effect=error), self.assertRaises(TranslationError) as raised:
                deepl_provider.translate(payload)
            self.assertEqual(raised.exception.code, code)


class LocalHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.delay = 0
        self.content = json.dumps({"title": "译文", "description": "正文"})
        self.done_reason = "stop"
        self.last_body = None
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def respond(self, value):
                body=json.dumps(value).encode()
                try:
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
            def do_GET(self):
                self.respond({"version": "test"} if self.path == "/api/version" else {"models": [{"name": "qwen3:8b", "digest": manifest()["model"]["digest"]}]})
            def do_POST(self):
                owner.last_body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                time.sleep(owner.delay)
                self.respond({"done": True, "done_reason": owner.done_reason, "message": {"content": owner.content}})
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.config={**DEFAULTS,"local_llm_mode":"external","local_llm_base_url":f"http://127.0.0.1:{self.server.server_port}"}
        self.payload={"provider":"local_llm","root":self.temp.name,"config":self.config,"source":{"title":"Title","description":"Body","source_lang":"en"}}

    def test_real_request_subprocess_uses_structured_no_thinking_request(self):
        result=isolated_request(self.payload,time.monotonic()+10)
        self.assertEqual(result["title"],"译文")
        self.assertFalse(self.last_body["think"])
        self.assertFalse(self.last_body["stream"])
        self.assertEqual(self.last_body["format"]["required"],["title","description"])

    def test_invalid_json_extra_fields_and_truncation_rejected(self):
        for content in ("```json\n{}\n```", '{}', '{"title":"","description":"a"}', '{"title":"a","description":"b","extra":"c"}'):
            self.content=content
            with self.subTest(content=content), self.assertRaises(TranslationError) as raised:
                isolated_request(self.payload,time.monotonic()+10)
            self.assertEqual(raised.exception.code,"OUTPUT_INVALID")
        self.content='{"title":"a","description":"b"}'
        self.done_reason="length"
        with self.assertRaises(TranslationError): isolated_request(self.payload,time.monotonic()+10)

    def test_hard_timeout_and_cancellation_kill_waiting_request(self):
        self.delay=2
        tick=time.monotonic()
        with self.assertRaises(TranslationError) as raised:
            isolated_request(self.payload,tick+.4)
        self.assertEqual(raised.exception.code,"TIMEOUT")
        self.assertLess(time.monotonic()-tick,2)
        cancel=threading.Event();timer=threading.Timer(.4,cancel.set);timer.start()
        with events.task_context("test",cancel,lambda *a:None), self.assertRaises(events.Cancelled):
            isolated_request(self.payload,time.monotonic()+10)
        timer.join()

    def test_digest_mismatch_never_infers(self):
        self.payload["expected_digest"]="wrong"
        with self.assertRaises(TranslationError) as raised:
            isolated_request(self.payload,time.monotonic()+10)
        self.assertEqual(raised.exception.code,"MODEL_DIGEST_MISMATCH")
        self.assertIsNone(self.last_body)

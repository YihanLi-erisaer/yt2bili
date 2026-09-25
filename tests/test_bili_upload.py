import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from yt2bili import bili_upload, events


class UploadArgumentTests(unittest.TestCase):
    def test_free_form_metadata_is_accepted_by_biliup_parser(self):
        root = Path(__file__).resolve().parents[1]
        try:
            executable = bili_upload.find_biliup(
                SimpleNamespace(biliup_bin=None, root=root, bin_dir=root / "bin")
            )
        except bili_upload.Yt2BiliError:
            self.skipTest("Local biliup executable is required for parser regression")

        def parse_only(cmd, **kwargs):
            # --help parses the real upload arguments and exits before login,
            # file reads or network activity. Never submit the test fixtures.
            result = subprocess.run(
                [*cmd, "--help"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Usage:", result.stdout)
            return result.returncode, result.stdout

        with tempfile.TemporaryDirectory() as folder:
            temp = Path(folder)
            video, cover, cookies = (temp / name for name in ("video.mp4", "cover.jpg", "cookies.json"))
            for file in (video, cover, cookies):
                file.touch()
            settings = SimpleNamespace(
                biliup_bin=executable, bili_cookies=cookies,
                bili_line="tx", bili_tid=171, bili_tags="模型,汽车",
            )
            cases = [
                ("模型汽车", "---__--- 🔔订阅频道\n原链接：https://www.youtube.com/watch?v=example", "模型,汽车"),
                ("--help", '-简介含 "引号"、空格、$7,000 和 = 等号', "--version"),
                ("普通标题", "", "模型"),
            ]
            with patch.object(bili_upload, "_run_logged", side_effect=parse_only):
                for title, description, tags in cases:
                    with self.subTest(title=title, description=description):
                        settings.bili_tags = tags
                        bili_upload.upload(settings, video, cover, title, description, "https://example.com")

    def test_upload_monitor_reports_speed_each_second(self):
        report = Mock()
        stop = Mock()
        stop.wait.side_effect = [False, False, True]
        state = {}
        with patch.object(bili_upload, "_process_read_bytes", side_effect=[100, 1100, 3100]), \
             patch.object(bili_upload.time, "monotonic", side_effect=[0, 1, 2]):
            bili_upload._monitor_upload(123, stop, report, state)
        self.assertEqual([call.kwargs["speed"] for call in report.call_args_list], [1000, 2000])
        self.assertEqual(state["speed"], 2000)

    def test_captured_progress_keeps_identity_in_monitor_thread(self):
        emitted = []
        with events.task_context("video-id", None, lambda event, payload: emitted.append((event, payload)),
                                 task_id="task-id", account_id="account-id", run_id="run-id"):
            report = events.capture_progress("uploading")
            thread = threading.Thread(target=lambda: report(speed=1024))
            thread.start()
            thread.join()
        self.assertEqual(emitted[0][0], "task.progress")
        self.assertEqual(emitted[0][1]["task_id"], "task-id")
        self.assertEqual(emitted[0][1]["stage"], "uploading")
        self.assertEqual(emitted[0][1]["speed"], 1024)


if __name__ == "__main__":
    unittest.main()

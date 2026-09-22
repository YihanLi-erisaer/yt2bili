import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from yt2bili import bili_upload


class UploadArgumentTests(unittest.TestCase):
    def test_free_form_metadata_is_accepted_by_biliup_parser(self):
        root = Path(__file__).resolve().parents[1]
        try:
            executable = bili_upload.find_biliup(
                SimpleNamespace(biliup_bin=None, root=root, bin_dir=root / "bin")
            )
        except bili_upload.Yt2BiliError:
            self.skipTest("Local biliup executable is required for parser regression")

        def parse_only(cmd):
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


if __name__ == "__main__":
    unittest.main()

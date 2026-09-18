import io
import logging
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from yt2bili import media, pipeline, youtube
from yt2bili.db import Task, TaskStore


class PipelineConcurrencyTests(unittest.TestCase):
    def test_second_download_runs_while_first_is_still_validating(self):
        validating = threading.Event()
        next_download = threading.Event()
        overlapped = []

        class Downloader:
            def __init__(self, opts):
                self.folder = Path(opts["outtmpl"]).parent

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, url, download):
                (self.folder / "source.mp4").write_bytes(b"downloaded fixture")
                if url == "second":
                    next_download.set()
                return {}

        def meta(url, settings):
            if url == "second":
                validating.wait(5)
            return youtube.YoutubeMeta(url, url, "title", "desc", "author", 10, "thumb", None, url)

        def validate(url, work, settings, source, duration):
            if url == "first":
                validating.set()
                overlapped.append(next_download.wait(5))
            return source

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = SimpleNamespace(work_dir=root / "work", download_jobs=1, upload_gap_seconds=0)
            store = TaskStore(root / "tasks.sqlite")
            try:
                for video_id in ("first", "second"):
                    work = settings.work_dir / video_id
                    work.mkdir(parents=True)
                    (work / "cover.jpg").write_bytes(b"cover")
                    store.upsert(Task(video_id, video_id, "pending", title_zh="title", desc_zh="desc"))
                with patch.object(pipeline.sys, "platform", "win32"), \
                     patch.object(youtube, "_download_lock", threading.Lock()), \
                     patch.object(media, "require_ffmpeg"), \
                     patch.object(youtube, "fetch_meta", side_effect=meta), \
                     patch.object(youtube, "_base_opts", return_value={}), \
                     patch.object(youtube, "_finish_existing_source", return_value=None), \
                     patch.object(youtube, "_log_selected_format"), \
                     patch.object(youtube.yt_dlp, "YoutubeDL", Downloader), \
                     patch.object(youtube, "_ensure_has_audio", side_effect=validate), \
                     patch.object(media, "prepare_upload_video", side_effect=lambda source, *a: source), \
                     patch.object(pipeline, "_upload_serialized") as upload:
                    results, failures = pipeline.run_many(settings, store, ["first", "second"], dry_run=True, jobs=1)
                self.assertEqual(overlapped, [True], "next download was blocked by the first validation")
                self.assertFalse(failures)
                self.assertEqual(len(results), 2)
                self.assertTrue(all(task.status == "ready" for task in results))
                upload.assert_not_called()
            finally:
                store.close()

    def test_download_slot_is_released_on_error(self):
        gate = threading.BoundedSemaphore(1)
        lock = threading.Lock()
        downloader = MagicMock()
        downloader.__enter__.return_value.extract_info.side_effect = RuntimeError("network error")
        with patch.object(youtube, "_download_lock", lock), \
             patch.object(youtube.yt_dlp, "YoutubeDL", return_value=downloader), \
             youtube.download_slots(gate):
            with self.assertRaises(RuntimeError):
                youtube._download_with_slot({}, "url")
            self.assertTrue(gate.acquire(blocking=False))
            gate.release()
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()

    def test_audio_download_uses_same_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def downloaded(opts, url):
                (root / "audio.m4a").write_bytes(b"audio")

            with patch.object(youtube, "_base_opts", return_value={}), \
                 patch.object(youtube, "_download_with_slot", side_effect=downloaded) as transfer:
                result = youtube._download_audio_only("url", root, None)
            self.assertEqual(result, root / "audio.m4a")
            transfer.assert_called_once()

    def test_batch_semaphore_really_bounds_parallel_downloads(self):
        gate = threading.BoundedSemaphore(2)
        two_active = threading.Event()
        release = threading.Event()
        mutex = threading.Lock()
        counts = {"active": 0, "peak": 0}

        class Downloader:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, *args, **kwargs):
                with mutex:
                    counts["active"] += 1
                    counts["peak"] = max(counts["peak"], counts["active"])
                    if counts["active"] == 2:
                        two_active.set()
                release.wait(5)
                with mutex:
                    counts["active"] -= 1
                return {}

        def run():
            with youtube.download_slots(gate):
                youtube._download_with_slot({}, "url")

        with patch.object(youtube, "_download_lock", None), \
             patch.object(youtube.yt_dlp, "YoutubeDL", Downloader), \
             ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(run) for _ in range(4)]
            try:
                self.assertTrue(two_active.wait(5))
                self.assertEqual(counts["peak"], 2)
            finally:
                release.set()
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(counts["peak"], 2)

    def test_gpu_progress_is_logged_periodically_with_video_id(self):
        process = MagicMock()
        process.stdout = io.StringIO("out_time_us=1000000\nout_time_us=5000000\n")
        process.wait.return_value = 0
        process.poll.return_value = 0
        with patch.object(media.subprocess, "Popen", return_value=process), \
             patch.object(media.time, "monotonic", side_effect=[0, 6, 6, 12, 12]), \
             self.assertLogs(media.logger, level=logging.INFO) as logs:
            result = media._decode_track(Path("first/source.mp4"), "v:0", 5, ["-hwaccel", "cuda"])
        self.assertEqual(result, 5)
        self.assertTrue(any("[first]" in line and "GPU" in line and "20.0%" in line for line in logs.output))
        self.assertTrue(any("100.0%" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()

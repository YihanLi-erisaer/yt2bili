import logging
import tempfile
import threading
import unittest
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from yt2bili import bili_upload, media, pipeline, youtube
from yt2bili.config import Settings
from yt2bili.db import Task, TaskStore
from yt2bili.exceptions import InvalidMediaError, Yt2BiliError


class StageQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = SimpleNamespace(work_dir=self.root / "work", download_jobs=8, upload_gap_seconds=20)
        self.store = TaskStore(self.root / "tasks.sqlite")
        self.addCleanup(self.store.close)
        self.downloads = Counter()
        self.uploaded = []
        for video_id in "ABCD":
            folder = self.settings.work_dir / video_id
            folder.mkdir(parents=True)
            (folder / "cover.jpg").write_bytes(b"cover")
            self.store.upsert(Task(video_id, video_id, "pending", title_zh="title", desc_zh="desc"))

    def meta(self, url, settings):
        return youtube.YoutubeMeta(url, url, "title", "desc", "author", 10, "thumb", None, url)

    def download(self, url, folder, settings, **kwargs):
        self.assertIs(kwargs["validate"], False)
        self.downloads[url] += 1
        source = folder / "source.mp4"
        source.write_bytes(b"fixture")
        return source

    def upload(self, settings, log, video, *args):
        self.assertTrue(video.exists())
        self.uploaded.append(video.parent.name)
        return "BV" + video.parent.name

    def run_batch(self, ids, *, download=None, validate=None, upload=None, meta=None, dry_run=False):
        with patch.object(media, "require_ffmpeg"), \
             patch.object(youtube, "fetch_meta", side_effect=meta or self.meta), \
             patch.object(youtube, "download_video", side_effect=download or self.download), \
             patch.object(media, "prepare_upload_video", side_effect=validate or (lambda source, *a: source)), \
             patch.object(pipeline, "_upload_serialized", side_effect=upload or self.upload):
            return pipeline.run_many(self.settings, self.store, list(ids), jobs=8, dry_run=dry_run)

    def test_three_stages_progress_independently_and_stay_single_lane(self):
        uploading_a = threading.Event()
        validating_b = threading.Event()
        downloaded_d = threading.Event()
        validated_c = threading.Event()
        mutex = threading.Lock()
        active, peak = Counter(), Counter()
        overlap = []
        validated = set()

        @contextmanager
        def stage(name):
            with mutex:
                active[name] += 1
                peak[name] = max(peak[name], active[name])
                overlap.append(sum(active.values()))
            try:
                yield
            finally:
                with mutex:
                    active[name] -= 1

        def download(url, *args, **kwargs):
            with stage("download"):
                if url == "C":
                    self.assertTrue(uploading_a.wait(5), "A upload never started")
                    self.assertTrue(validating_b.wait(5), "B validation never started")
                    with mutex:
                        overlap.append(sum(active.values()))
                source = self.download(url, *args, **kwargs)
                if url == "D":
                    downloaded_d.set()
                return source

        def validate(source, *args):
            with stage("validate"):
                video_id = source.parent.name
                if video_id == "B":
                    validating_b.set()
                    self.assertTrue(downloaded_d.wait(5), "later downloads were blocked by validation")
                validated.add(video_id)
                if video_id == "C":
                    validated_c.set()
                return source

        def upload(settings, log, video, *args):
            with stage("upload"):
                self.assertIn(video.parent.name, validated)
                if video.parent.name == "A":
                    uploading_a.set()
                    self.assertTrue(validated_c.wait(5), "validation was blocked by A upload")
                return self.upload(settings, log, video, *args)

        results, failures = self.run_batch("ABCD", download=download, validate=validate, upload=upload)
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 4)
        self.assertEqual(peak, {"download": 1, "validate": 1, "upload": 1})
        self.assertEqual(max(overlap), 3)
        self.assertEqual(self.uploaded, list("ABCD"))

    def test_failed_validation_returns_to_download_queue(self):
        attempts = Counter()

        def validate(source, *args):
            video_id = source.parent.name
            attempts[video_id] += 1
            if video_id == "A" and attempts[video_id] == 1:
                raise InvalidMediaError("truncated")
            return source

        results, failures = self.run_batch("AB", validate=validate)
        self.assertFalse(failures)
        self.assertEqual(len(results), 2)
        self.assertEqual(self.downloads["A"], 2)
        self.assertEqual(self.uploaded.count("A"), 1)
        self.assertEqual(self.uploaded.count("B"), 1)

    def test_slow_submission_preparation_does_not_hold_validation_slot(self):
        validated_c = threading.Event()
        prepare = pipeline._prepare_assets

        def validate(source, *args):
            if source.parent.name == "C":
                validated_c.set()
            return source

        def prepare_slow(settings, store, task, *args):
            if task.video_id == "A":
                self.assertTrue(validated_c.wait(5), "cover/translation blocked the validation queue")
            return prepare(settings, store, task, *args)

        with patch.object(pipeline, "_prepare_assets", side_effect=prepare_slow):
            results, failures = self.run_batch("ABC", validate=validate)
        self.assertFalse(failures)
        self.assertEqual(len(results), 3)

    def test_permanent_validation_failure_is_bounded_and_never_uploaded(self):
        def validate(source, *args):
            if source.parent.name == "A":
                raise InvalidMediaError("broken")
            return source

        results, failures = self.run_batch("AB", validate=validate)
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.downloads["A"], pipeline.MAX_MEDIA_ATTEMPTS)
        self.assertEqual(self.uploaded, ["B"])
        self.assertEqual(self.store.require("A").status, "failed")
        self.assertTrue((self.settings.work_dir / "A").exists())

    def test_download_failure_does_not_stop_other_jobs(self):
        def download(url, *args, **kwargs):
            if url == "A":
                raise Yt2BiliError("network failed")
            return self.download(url, *args, **kwargs)

        _, failures = self.run_batch("AB", download=download)
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.uploaded, ["B"])
        self.assertEqual(self.store.require("A").status, "failed")

    def test_upload_failure_preserves_files_and_queue_continues(self):
        def upload(settings, log, video, *args):
            if video.parent.name == "A":
                raise Yt2BiliError("upload failed")
            return self.upload(settings, log, video, *args)

        _, failures = self.run_batch("AB", upload=upload)
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.uploaded, ["B"])
        self.assertTrue((self.settings.work_dir / "A" / "source.mp4").exists())
        self.assertFalse((self.settings.work_dir / "B").exists())

    def test_duplicate_video_ids_do_not_race_or_upload_twice(self):
        results, failures = self.run_batch(["A", "alias"], meta=lambda url, settings: self.meta("A", settings))
        self.assertEqual(len(results), 1)
        self.assertFalse(failures)
        self.assertEqual(self.downloads["A"], 1)
        self.assertEqual(self.uploaded, ["A"])

    def test_dry_run_never_uploads_or_deletes(self):
        results, failures = self.run_batch("AB", dry_run=True)
        self.assertFalse(failures)
        self.assertEqual(self.uploaded, [])
        self.assertTrue(all(task.status == "ready" for task in results))
        self.assertTrue((self.settings.work_dir / "A" / "source.mp4").exists())

    def test_already_submitted_is_skipped_without_deleting_files(self):
        task = self.store.require("A")
        task.status, task.bv_id = "submitted", "BVexisting"
        self.store.upsert(task)
        results, failures = self.run_batch("A")
        self.assertEqual(results[0].bv_id, "BVexisting")
        self.assertFalse(failures)
        self.assertFalse(self.downloads)
        self.assertTrue((self.settings.work_dir / "A").exists())

    def test_upload_gap_is_twenty_seconds_after_previous_completion(self):
        self.assertEqual(Settings.__dataclass_fields__["upload_gap_seconds"].default, 20)
        with patch.object(pipeline, "_last_upload_monotonic", 100), \
             patch.object(pipeline.time, "monotonic", side_effect=[105, 130]), \
             patch.object(pipeline.time, "sleep") as sleep, \
             patch.object(bili_upload, "renew"), \
             patch.object(bili_upload, "upload", return_value="BVtest"):
            result = pipeline._upload_serialized(self.settings, logging.getLogger(), Path("video"), Path("cover"), "title", "desc", "url")
            self.assertEqual(result, "BVtest")
            sleep.assert_called_once_with(15)
            self.assertEqual(pipeline._last_upload_monotonic, 130)


if __name__ == "__main__":
    unittest.main()

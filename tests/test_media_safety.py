import subprocess
import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from yt2bili import media, pipeline, youtube
from yt2bili.db import Task
from yt2bili.exceptions import InvalidMediaError, Yt2BiliError


class MediaSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            media.require_ffmpeg()
        except Yt2BiliError as exc:
            raise unittest.SkipTest(str(exc))
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.good = cls.root / "good.mp4"
        cls.av1 = cls.root / "av1.mp4"
        cls.short_video = cls.root / "short-video.mp4"
        cls.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-movflags", "+faststart", str(cls.good),
        )
        cls.ffmpeg("-i", str(cls.good), "-c:v", "libaom-av1", "-cpu-used", "8", "-crf", "40", "-c:a", "copy", str(cls.av1))
        cls.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-c:v", "libx264", "-c:a", "aac", str(cls.short_video),
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def ffmpeg(*args):
        subprocess.run([media.ffmpeg_tool("ffmpeg"), "-nostdin", "-y", "-v", "error", *args], check=True, capture_output=True)

    def setUp(self):
        media._validated.clear()

    def test_valid_h264_aac_passes(self):
        self.assertEqual(media.validate_media(self.good, 3)["vcodec"], "h264")

    def test_mp4_upload_uses_original_bytes_without_transcoding(self):
        for source in (self.good, self.av1):
            with self.subTest(source=source.name):
                before = source.read_bytes()
                dest = self.root / (source.stem + "-upload.mp4")
                with patch.object(media, "ensure_bilibili_mp4") as convert:
                    result = media.prepare_upload_video(source, dest, 3)
                    convert.assert_not_called()
                self.assertEqual(result, source)
                self.assertEqual(source.read_bytes(), before)
                self.assertFalse(dest.exists())

    def test_oversize_mp4_is_not_silently_transcoded(self):
        with patch.object(media, "ensure_bilibili_mp4") as convert:
            with self.assertRaises(Yt2BiliError):
                media.prepare_upload_video(self.av1, self.root / "oversize.mp4", 3, max_size_gb=0.000001)
            convert.assert_not_called()

    def test_invalid_mp4_cannot_be_used_directly(self):
        with self.assertRaises(InvalidMediaError):
            media.prepare_upload_video(self.short_video, self.root / "invalid-upload.mp4", 10)

    def test_non_mp4_still_converts(self):
        source = self.root / "source.mkv"
        dest = self.root / "from-mkv.mp4"
        self.ffmpeg("-i", str(self.av1), "-c", "copy", str(source))
        self.assertEqual(media.prepare_upload_video(source, dest, 3), dest)
        self.assertTrue(media._upload_compatible(media.validate_media(dest, 3)))

    def test_long_audio_does_not_hide_missing_picture(self):
        self.assertGreater(media.probe_brief(self.short_video)["duration"], 9)
        with self.assertRaises(InvalidMediaError):
            media.validate_media(self.short_video, 10)

    def test_truncated_file_with_intact_duration_header_rejected(self):
        damaged = self.root / "truncated.mp4"
        data = self.good.read_bytes()
        damaged.write_bytes(data[:len(data) // 2])
        # faststart keeps the original duration at the beginning of the file.
        info = media.probe_brief(damaged)
        if info:
            self.assertAlmostEqual(info["duration"], 3, delta=0.1)
        with self.assertRaises(InvalidMediaError):
            media.validate_media(damaged, 3)

    def test_av1_inside_mp4_is_converted_to_h264(self):
        dest = self.root / "converted.mp4"
        media.ensure_bilibili_mp4(self.av1, dest, 3)
        info = media.validate_media(dest, 3)
        self.assertEqual((info["vcodec"], info["acodec"], info["pix_fmt"]), ("h264", "aac", "yuv420p"))
        self.assertEqual((info["width"], info["height"]), (160, 90))

    def test_existing_invalid_output_is_not_skipped(self):
        with self.assertRaises(InvalidMediaError):
            media.ensure_bilibili_mp4(self.short_video, self.short_video, 10)

    def test_existing_av1_can_be_repaired_in_place(self):
        dest = self.root / "in-place.mp4"
        dest.write_bytes(self.av1.read_bytes())
        media.ensure_bilibili_mp4(dest, dest, 3)
        self.assertEqual(media.validate_media(dest, 3)["vcodec"], "h264")

    def test_compatible_video_is_stream_copied(self):
        dest = self.root / "copied.mp4"
        with patch.object(media, "_run_ffmpeg", wraps=media._run_ffmpeg) as run:
            media.ensure_bilibili_mp4(self.good, dest, 3)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")

    def test_failed_conversion_preserves_destination(self):
        dest = self.root / "preserved.mp4"
        dest.write_bytes(b"old incomplete output")
        with patch.object(media, "_run_ffmpeg", side_effect=Yt2BiliError("encoder failed")):
            with self.assertRaises(Yt2BiliError):
                media.ensure_bilibili_mp4(self.av1, dest, 3)
        self.assertEqual(dest.read_bytes(), b"old incomplete output")

    def test_optional_size_budget_preserves_resolution(self):
        dest = self.root / "budget.mp4"
        media.ensure_bilibili_mp4(self.av1, dest, 3, max_size_gb=0.0005)
        self.assertLessEqual(dest.stat().st_size, 500_000)
        info = media.validate_media(dest, 3)
        self.assertEqual((info["width"], info["height"]), (160, 90))

    def test_impossible_size_budget_does_not_create_output(self):
        dest = self.root / "too-small.mp4"
        with self.assertRaises(Yt2BiliError):
            media.ensure_bilibili_mp4(self.av1, dest, 3, max_size_gb=0.000001)
        self.assertFalse(dest.exists())

    def test_mux_checks_tracks_and_copies_existing_aac(self):
        audio = self.root / "audio.m4a"
        video = self.root / "video-only.mp4"
        dest = self.root / "muxed.mp4"
        self.ffmpeg("-i", str(self.good), "-vn", "-c:a", "copy", str(audio))
        self.ffmpeg("-i", str(self.good), "-an", "-c:v", "copy", str(video))
        with patch.object(media, "_run_ffmpeg", wraps=media._run_ffmpeg) as run:
            media.mux_video_audio(video, audio, dest, 3)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")
        self.assertTrue(media.validate_media(dest, 3)["has_audio"])

    def test_nvidia_encoder_produces_valid_upload_when_available(self):
        check = subprocess.run([
            media.ffmpeg_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
            "color=size=160x90", "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-",
        ], capture_output=True)
        if check.returncode:
            self.skipTest("NVIDIA encoder unavailable")
        dest = self.root / "gpu.mp4"
        media.ensure_bilibili_mp4(self.av1, dest, 3, encoder="h264_nvenc")
        self.assertTrue(media._upload_compatible(media.validate_media(dest, 3)))

    def test_optional_cuda_full_decode_and_conversion(self):
        source = self.root / "cuda-source.mp4"
        self.ffmpeg("-i", str(self.good), "-vf", "scale=320:240", "-c:v", "libaom-av1", "-cpu-used", "8", "-crf", "40", "-c:a", "copy", str(source))
        check = subprocess.run([
            media.ffmpeg_tool("ffmpeg"), "-v", "error", "-hwaccel", "cuda",
            "-hwaccel_output_format", "cuda", "-c:v", "av1_cuvid", "-i", str(source),
            "-frames:v", "1", "-an", "-f", "null", "-",
        ], capture_output=True)
        if check.returncode:
            self.skipTest("AV1 NVIDIA decoder unavailable")
        with patch.dict(os.environ, {"YT2BILI_HWACCEL": "cuda"}):
            dest = self.root / "cuda.mp4"
            media.ensure_bilibili_mp4(source, dest, 3, encoder="h264_nvenc")
            self.assertTrue(media._upload_compatible(media.validate_media(dest, 3)))
            with self.assertRaises(InvalidMediaError):
                media.validate_media(self.short_video, 10)


class ValidationAccelerationCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source.mp4"
        MediaSafetyTests.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=duration=3", "-c:v", "libx264",
            "-c:a", "aac", str(self.source),
        )
        media._validated.clear()
        self.environment = patch.dict(os.environ, {"YT2BILI_HWACCEL": "auto", "YT2BILI_VALIDATION_CACHE": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_auto_tries_gpu_then_cpu_on_failure(self):
        with patch.object(media, "_decode_track", side_effect=[InvalidMediaError("no device"), 3.0, 3.0]) as decode:
            media.validate_media(self.source, 3)
        calls = decode.call_args_list
        self.assertIn("cuda", calls[0].args[3])
        self.assertEqual(calls[1].args[1:], ("v:0", 3, []))
        self.assertEqual(calls[2].args[1:], ("a:0", 3, []))
        self.assertTrue(media._validation_cache_path(self.source).exists())

    def test_gpu_and_cpu_failure_never_saved_as_pass(self):
        with patch.object(media, "_decode_track", side_effect=InvalidMediaError("broken")) as decode:
            with self.assertRaises(InvalidMediaError):
                media.validate_media(self.source, 3)
        self.assertEqual(decode.call_count, 2)
        self.assertFalse(media._validation_cache_path(self.source).exists())

    def test_force_cpu_never_tries_cuda(self):
        with patch.dict(os.environ, {"YT2BILI_HWACCEL": "cpu"}), \
             patch.object(media, "_decode_track", return_value=3.0) as decode:
            media.validate_media(self.source, 3)
        self.assertTrue(all(not call.args[3] for call in decode.call_args_list))

    def test_unsupported_codec_uses_cpu(self):
        info = media.probe_brief(self.source)
        info["vcodec"] = "unsupported"
        with patch.object(media, "probe_brief", return_value=info), \
             patch.object(media, "_decode_track", return_value=3.0) as decode:
            media.validate_media(self.source, 3)
        self.assertTrue(all(not call.args[3] for call in decode.call_args_list))

    def test_real_cache_reused_by_fresh_python_process(self):
        media.validate_media(self.source, 3)
        code = (
            "import sys; from pathlib import Path; from unittest.mock import patch; "
            "from yt2bili import media; "
            "p=patch.object(media, '_decode_track', side_effect=AssertionError('unexpected decode')); "
            "p.start(); media.validate_media(Path(sys.argv[1]), 3)"
        )
        result = subprocess.run([sys.executable, "-c", code, str(self.source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_same_size_middle_change_with_restored_mtime_invalidates(self):
        with patch.object(media, "_decode_track", return_value=3.0):
            media.validate_media(self.source, 3)
        info = media.probe_brief(self.source)
        stat = self.source.stat()
        payload = bytearray(self.source.read_bytes())
        payload[len(payload) // 2] ^= 1
        self.source.write_bytes(payload)
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        # Hide stat changes too, so this specifically tests the full SHA-256.
        record = json.loads(media._validation_cache_path(self.source).read_text())
        record["fingerprint"]["state"] = list(media._file_state(self.source))
        media._validation_cache_path(self.source).write_text(json.dumps(record))
        media._validated.clear()
        with patch.object(media, "probe_brief", return_value=info), \
             patch.object(media, "_decode_track", return_value=3.0) as decode:
            media.validate_media(self.source, 3)
        self.assertEqual(decode.call_count, 2)

    def test_expected_duration_and_rule_version_invalidate(self):
        with patch.object(media, "_decode_track", return_value=3.0):
            media.validate_media(self.source, 3)
        with patch.object(media, "_decode_track", return_value=3.0) as decode:
            media.validate_media(self.source, 4)
        self.assertEqual(decode.call_count, 2)
        with patch.object(media, "_VALIDATION_VERSION", media._VALIDATION_VERSION + 1), \
             patch.object(media, "_decode_track", return_value=3.0) as decode:
            media.validate_media(self.source, 4)
        self.assertEqual(decode.call_count, 2)

    def test_tool_change_invalidates_cache(self):
        with patch.object(media, "_decode_track", return_value=3.0):
            media.validate_media(self.source, 3)
        fingerprint = media._validation_fingerprint(self.source, 3)
        fingerprint["tools"][0][1][1] += 1
        with patch.object(media, "_validation_fingerprint", return_value=fingerprint), \
             patch.object(media, "_decode_track", return_value=3.0) as decode:
            media.validate_media(self.source, 3)
        self.assertEqual(decode.call_count, 2)

    def test_bad_json_cache_is_ignored(self):
        media._validation_cache_path(self.source).write_text("{partial")
        with patch.object(media, "_decode_track", return_value=3.0) as decode:
            media.validate_media(self.source, 3)
        self.assertEqual(decode.call_count, 2)

    def test_all_tracks_cache_reused_when_audio_becomes_required(self):
        with patch.object(media, "_decode_track", return_value=3.0):
            media.validate_media(self.source, 3, require_audio=False)
        media._validated.clear()
        with patch.object(media, "_decode_track") as decode:
            media.validate_media(self.source, 3, require_audio=True)
        decode.assert_not_called()

    def test_video_only_cache_cannot_bypass_audio_requirement(self):
        info = media.probe_brief(self.source)
        info["has_audio"] = False
        info["audio_duration"] = None
        with patch.object(media, "probe_brief", return_value=info), \
             patch.object(media, "_decode_track", return_value=3.0):
            media.validate_media(self.source, 3, require_audio=False)
            with self.assertRaises(InvalidMediaError):
                media.validate_media(self.source, 3, require_audio=True)

    def test_cache_can_be_disabled(self):
        with patch.dict(os.environ, {"YT2BILI_VALIDATION_CACHE": "0"}), \
             patch.object(media, "_decode_track", return_value=3.0) as decode:
            media.validate_media(self.source, 3)
            media.validate_media(self.source, 3)
        self.assertEqual(decode.call_count, 4)
        self.assertFalse(media._validation_cache_path(self.source).exists())

    def test_audio_failure_does_not_cache_successful_video(self):
        with patch.object(media, "_decode_track", side_effect=[3.0, InvalidMediaError("bad audio")]):
            with self.assertRaises(InvalidMediaError):
                media.validate_media(self.source, 3)
        self.assertFalse(media._validation_cache_path(self.source).exists())

    def test_interruption_does_not_cache_or_retry(self):
        with patch.object(media, "_decode_track", side_effect=KeyboardInterrupt) as decode:
            with self.assertRaises(KeyboardInterrupt):
                media.validate_media(self.source, 3)
        self.assertEqual(decode.call_count, 1)
        self.assertFalse(media._validation_cache_path(self.source).exists())

    def test_file_changed_while_decoding_is_not_cached(self):
        def changed(*args):
            with self.source.open("ab") as stream:
                stream.write(b"new data")
            return 3.0
        with patch.object(media, "_decode_track", side_effect=changed):
            with self.assertRaises(Yt2BiliError):
                media.validate_media(self.source, 3)
        self.assertFalse(media._validation_cache_path(self.source).exists())

    def test_cache_write_failure_does_not_reject_valid_media(self):
        with patch.object(media, "_decode_track", return_value=3.0), \
             patch.object(media.tempfile, "NamedTemporaryFile", side_effect=PermissionError("read only")):
            self.assertTrue(media.validate_media(self.source, 3)["has_video"])


class DownloadRecoveryTests(unittest.TestCase):
    def test_part_file_never_promoted_by_header_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            part = root / "source.f401.mp4.part"
            part.write_bytes(b"incomplete download")
            with patch.object(media, "validate_media") as validate:
                self.assertIsNone(youtube._best_complete_video(root, 5127))
                validate.assert_not_called()
            self.assertTrue(part.exists())
            self.assertFalse((root / "source.f401.mp4").exists())

    def test_invalid_source_is_preserved_but_not_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            source.write_bytes(b"damaged")
            with patch.object(media, "validate_media", side_effect=InvalidMediaError("truncated")):
                self.assertIsNone(youtube._best_complete_video(root, 10))
            self.assertFalse(source.exists())
            self.assertEqual(next((root / "rejected").rglob("source.mp4")).read_bytes(), b"damaged")

    def test_interrupted_mux_output_not_selected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source.mp4.tmp.mp4").write_bytes(b"temporary")
            self.assertIsNone(youtube._find_source(root))
            with patch.object(media, "validate_media") as validate:
                self.assertIsNone(youtube._best_complete_video(root, 10))
                validate.assert_not_called()

    def test_416_recovery_preserves_partial_download(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source.mp4.part").write_bytes(b"recoverable")
            self.assertIsNone(youtube._recover_after_416("url", root, None, 10))
            self.assertEqual(next((root / "rejected").rglob("source.mp4.part")).read_bytes(), b"recoverable")

    def test_cached_invalid_video_cannot_reach_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "video.mp4").write_bytes(b"bad cached upload")
            meta = youtube.YoutubeMeta("id", "url", "title", "desc", "author", 10, "thumb", None, "url")
            task = Task("id", "url", "failed")
            with patch.object(media, "prepare_upload_video", side_effect=InvalidMediaError("bad cache")), \
                 patch.object(youtube, "download_video", side_effect=InvalidMediaError("bad download")) as download, \
                 patch.object(pipeline, "_upload_serialized") as upload:
                with self.assertRaises(InvalidMediaError):
                    pipeline._execute(SimpleNamespace(), MagicMock(), task, meta, root, dry_run=False)
                download.assert_called_once()
                upload.assert_not_called()
            self.assertFalse((root / "video.mp4").exists())

    def test_repair_reuses_valid_file_without_republishing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            work = root / "id"
            work.mkdir()
            video = work / "video.mp4"
            video.write_bytes(b"valid fixture")
            task = Task("id", "url", "submitted", bv_id="BVexisting")
            store = MagicMock()
            store.require.return_value = task
            meta = youtube.YoutubeMeta("id", "url", "title", "desc", "author", 10, "thumb", None, "url")
            with patch.object(media, "require_ffmpeg"), \
                 patch.object(youtube, "fetch_meta", return_value=meta), \
                 patch.object(media, "prepare_upload_video", return_value=video), \
                 patch.object(youtube, "download_video") as download, \
                 patch.object(pipeline, "_upload_serialized") as upload:
                self.assertEqual(pipeline.repair(SimpleNamespace(work_dir=root), store, "id"), video)
                download.assert_not_called()
                upload.assert_not_called()
            self.assertEqual((task.status, task.bv_id), ("submitted", "BVexisting"))
            self.assertEqual(task.video_path, str(video))
            store.upsert.assert_called_once_with(task)


class UploadCleanupTests(unittest.TestCase):
    def run_pipeline(self, root, *, dry_run=False, bv="BVtest", failure=None):
        work = root / "id"
        work.mkdir()
        source = work / "source.mp4"
        source.write_bytes(b"validated source")
        (work / "cover.jpg").write_bytes(b"cover")
        task = Task("id", "url", "pending", title_zh="title", desc_zh="description")
        meta = youtube.YoutubeMeta("id", "url", "title", "desc", "author", 10, "thumb", None, "url")
        with patch.object(youtube, "download_video", return_value=source), \
             patch.object(media, "prepare_upload_video", return_value=source), \
             patch.object(pipeline, "_upload_serialized", return_value=bv, side_effect=failure) as upload:
            if failure:
                with self.assertRaises(Yt2BiliError):
                    pipeline._execute(SimpleNamespace(work_dir=root), MagicMock(), task, meta, work, dry_run=dry_run)
            else:
                pipeline._execute(SimpleNamespace(work_dir=root), MagicMock(), task, meta, work, dry_run=dry_run)
            if dry_run:
                upload.assert_not_called()
            else:
                self.assertEqual(upload.call_args.args[2], source)
        return work, task

    def test_success_cleans_only_current_task_after_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            other = root / "other.mp4"
            other.write_bytes(b"keep")
            work, task = self.run_pipeline(root)
            self.assertFalse(work.exists())
            self.assertTrue(other.exists())
            self.assertEqual((task.status, task.bv_id), ("submitted", "BVtest"))
            self.assertEqual((task.work_dir, task.video_path, task.cover_path), ("", "", ""))

    def test_failed_upload_retains_files(self):
        with tempfile.TemporaryDirectory() as folder:
            work, _ = self.run_pipeline(Path(folder), failure=Yt2BiliError("upload failed"))
            self.assertTrue((work / "source.mp4").exists())

    def test_dry_run_retains_files(self):
        with tempfile.TemporaryDirectory() as folder:
            work, task = self.run_pipeline(Path(folder), dry_run=True)
            self.assertTrue((work / "source.mp4").exists())
            self.assertEqual(task.status, "ready")

    def test_missing_bv_retains_files(self):
        with tempfile.TemporaryDirectory() as folder:
            work, task = self.run_pipeline(Path(folder), bv="")
            self.assertTrue((work / "source.mp4").exists())
            self.assertTrue(task.video_path)

    def test_cleanup_failure_keeps_submitted_status_and_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(pipeline.shutil, "rmtree", side_effect=OSError("file locked")):
                work, task = self.run_pipeline(Path(folder))
            self.assertTrue((work / "source.mp4").exists())
            self.assertEqual(task.status, "submitted")
            self.assertTrue(task.video_path)

    def test_cleanup_refuses_root_and_outside_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "work"
            root.mkdir()
            outside = Path(folder) / "outside"
            outside.mkdir()
            pipeline._remove_work_dir(root, root)
            pipeline._remove_work_dir(root, outside)
            self.assertTrue(root.exists())
            self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()

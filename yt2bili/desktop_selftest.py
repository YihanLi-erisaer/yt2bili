"""Offline frozen-runtime smoke test. No cookies, network or real submissions."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from yt2bili import media, pipeline
from yt2bili.config import Settings
from yt2bili.db import Task, TaskStore
from yt2bili.process_manager import creation_options
from yt2bili.youtube import YoutubeMeta


def run(paths):
    os.environ["YT2BILI_BIN_DIR"] = str(paths.resources / "bin")
    os.environ["YT2BILI_HWACCEL"] = "cpu"
    media.require_ffmpeg()
    with tempfile.TemporaryDirectory(prefix="media-smoke-", dir=paths.root) as folder:
        root = Path(folder)
        work = root / "work" / "smoketest01"
        work.mkdir(parents=True)
        video = work / "video.mp4"
        subprocess.run([media.ffmpeg_tool("ffmpeg"), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=320x240:rate=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                        "-t", "5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)],
                       check=True, timeout=30, **creation_options())
        media.extract_frame_cover(video, work / "cover.jpg")
        settings = Settings(root, "", root / "no-cookie.json", None, None, None, 171, "转载", "tx",
                            root / "work", root / "data", paths.resources / "bin")
        store = TaskStore(root / "data/tasks.sqlite")
        try:
            task = Task("smoketest01", "https://www.youtube.com/watch?v=smoketest01", "pending")
            meta = YoutubeMeta(task.video_id, task.url, "本地测试素材", "此素材由 FFmpeg 生成。", "本地测试", 5,
                               "", "zh", task.url)
            pipeline._execute(settings, store, task, meta, work, dry_run=True)
            assert task.status == "ready" and video.is_file() and not task.bv_id
            assert (work / "cover.jpg").is_file() and (work / "title.txt").is_file()
            media.validate_media(video, 5)
            return {"ok": True, "status": task.status, "media_validation": "CPU + SHA256 cache",
                    "network_used": False, "submitted": False}
        finally:
            pipeline._detach_file_handler(work / "pipeline.log", task.video_id)
            store.close()

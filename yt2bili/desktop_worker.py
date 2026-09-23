"""Versioned JSON Lines worker. stdout is exclusively protocol output."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from yt2bili import events
from yt2bili.exceptions import Yt2BiliError
from yt2bili.locking import FileLock
from yt2bili.paths import AppPaths
from yt2bili.process_manager import own_children


MAX_MESSAGE = 1_048_576


def redact(text):
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", str(text))
    if re.search(r"(?i)(sessdata|bili_jct|access_token|refresh_token|auth_code|auth_key|authorization|set-cookie|cookie_info)\s*[=:：\"']", text):
        return "[包含凭据信息的日志已隐藏]"
    text = re.sub(r"\b[0-9a-fA-F-]{30,}:fx\b", "[密钥已隐藏]", text)
    return text[:4000]


class DesktopLogHandler(logging.Handler):
    def __init__(self, service, file_handler):
        super().__init__()
        self.service, self.file_handler = service, file_handler

    def emit(self, record):
        try:
            message = redact(record.getMessage())
            video_id = events.current_video_id()
            if not video_id:
                match = re.search(r"\[([A-Za-z0-9_-]{11})\]", message)
                video_id = match.group(1) if match else None
            self.service.add_log({"time": time.strftime("%H:%M:%S"), "level": record.levelname,
                                  "video_id": video_id, **events.current_identity(), "message": message})
            safe = logging.LogRecord(record.name, record.levelno, "", 0, message, (), None)
            self.file_handler.emit(safe)
        except Exception:
            self.handleError(record)


class Protocol:
    def __init__(self, stream):
        self.stream, self.lock = stream, threading.RLock()
        self.session_id = str(uuid.uuid4())
        self.sequence = 0

    def write(self, value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        with self.lock:
            self.stream.write(data)
            self.stream.flush()

    def emit(self, event, payload):
        with self.lock:
            self.sequence += 1
            sequence = self.sequence
            self.write({"protocol_version": 2, "event": event, "event_id": sequence,
                        "worker_session_id": self.session_id, "time": time.time(), "payload": payload})

    def handle(self, service, request):
        request_id = request.get("request_id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict) or request.get("protocol_version") != 2:
                raise Yt2BiliError("协议版本不兼容。")
            if not isinstance(request_id, str) or len(request_id) > 100:
                raise Yt2BiliError("缺少有效请求 ID。")
            result = service.dispatch(request.get("method"), request.get("params", {}))
            self.write({"request_id": request_id, "result": result})
        except Exception as exc:
            message = str(exc) if isinstance(exc, Yt2BiliError) else "操作未完成，请检查输入、文件权限或网络。"
            self.write({"request_id": request_id, "error": {"code": type(exc).__name__, "message": redact(message)}})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    parser.add_argument("--resources")
    parser.add_argument("--self-test", action="store_true", help="Run an offline media dry-run using generated test media")
    args = parser.parse_args()
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    protocol = Protocol(sys.stdout)
    sys.stdout = sys.stderr
    paths = AppPaths.default(args.data_dir, args.resources)
    if args.self_test:
        from yt2bili.desktop_selftest import run
        protocol.write(run(paths))
        return 0
    with own_children(), FileLock(paths.root / "desktop.lock"):
        from yt2bili.desktop_service import DesktopService
        service = DesktopService(paths, protocol.emit)
        file_handler = logging.handlers.RotatingFileHandler(paths.root / "logs/desktop.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        handler = DesktopLogHandler(service, file_handler)
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
        protocol.emit("worker.ready", service.health())
        pending = threading.BoundedSemaphore(16)
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="desktop-rpc") as pool:
            while True:
                line = sys.stdin.readline(MAX_MESSAGE + 1)
                if not line:
                    break
                if len(line) > MAX_MESSAGE:
                    protocol.write({"request_id": None, "error": {"code": "MessageTooLarge", "message": "消息超过限制，连接已关闭。"}})
                    break
                try:
                    request = json.loads(line)
                except ValueError:
                    protocol.write({"request_id": None, "error": {"code": "InvalidJSON", "message": "消息不是有效 JSON。"}})
                    continue
                pending.acquire()
                future = pool.submit(protocol.handle, service, request)
                future.add_done_callback(lambda _: pending.release())
        service.close()
        logging.getLogger().removeHandler(handler)
        file_handler.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

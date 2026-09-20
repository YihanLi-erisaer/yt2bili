from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
from collections import deque
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

from yt2bili import bili_upload, youtube, translate
from yt2bili.db import Task, TaskStore
from yt2bili.desktop_auth import LoginSession, account_status, validate_login
from yt2bili.desktop_settings import DesktopSettings, atomic_json
from yt2bili.exceptions import Yt2BiliError
from yt2bili.process_manager import creation_options
from yt2bili.scheduler import Scheduler
from yt2bili.tools import find_tool


VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_urls(text):
    if not isinstance(text, str) or len(text) > 250_000:
        raise Yt2BiliError("链接列表过大或格式无效。")
    items, errors, seen = [], [], set()
    for index, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = urlparse(line)
        host = (parsed.hostname or "").lower()
        video_id = ""
        if parsed.scheme in ("https", "http") and not parsed.username and not parsed.password:
            if host in ("youtu.be", "www.youtu.be"):
                video_id = parsed.path.strip("/")
            elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
                if parsed.path == "/watch":
                    video_id = parse_qs(parsed.query).get("v", [""])[0]
                elif parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
                    video_id = parsed.path.split("/")[2]
        if not VIDEO_ID.fullmatch(video_id):
            errors.append(f"第 {index} 行不是有效的 YouTube 视频链接")
        elif video_id not in seen:
            seen.add(video_id)
            items.append((video_id, "https://www.youtube.com/watch?v=" + video_id))
    if errors:
        raise Yt2BiliError("；".join(errors[:12]))
    if not items or len(items) > 200:
        raise Yt2BiliError("每批请输入 1～200 条视频链接。")
    return items


class DesktopService:
    def __init__(self, paths, emit, vault=None):
        self.paths, self.output = paths, emit
        self.logs = deque(maxlen=1500)
        self.logs_lock = threading.Lock()
        self.mutation = threading.RLock()
        self.login = None
        self.auth_state = {"status": "idle"}
        self.config = DesktopSettings(paths, vault)
        self.store = TaskStore(paths.root / "data/tasks.sqlite", self.emit)
        self.scheduler = Scheduler(self.store, self.config, self.emit)
        os.environ["YT2BILI_BIN_DIR"] = str(paths.resources / "bin")
        self.apply_environment()

    def apply_environment(self):
        os.environ["YT2BILI_HWACCEL"] = self.config.values["hwaccel"]
        os.environ["YT2BILI_VALIDATION_CACHE"] = "1" if self.config.values["validation_cache"] else "0"

    def emit(self, event, payload):
        if event == "auth.status":
            if self.login and payload.get("session_id") != self.login.session_id:
                return
            self.auth_state = {**self.auth_state, **payload}
            if payload.get("status") not in ("waiting", "scanned"):
                self.auth_state.pop("qrcode", None)
        self.output(event, payload)

    def add_log(self, entry):
        with self.logs_lock:
            self.logs.append(entry)
        self.output("task.log", entry)

    def ensure_idle(self):
        if self.scheduler.snapshot()["active"]:
            raise Yt2BiliError("请等待当前任务结束后修改设置或账号。")

    def task(self, video_id):
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            raise Yt2BiliError("视频 ID 无效。")
        return self.store.require(video_id)

    def ensure_inactive(self, video_id):
        if video_id in self.scheduler.active:
            raise Yt2BiliError("任务正在执行，请先等待或取消。")

    def ensure_login_finished(self):
        if self.login and self.login.thread and self.login.thread.is_alive() and not self.login.cancelled.is_set():
            raise Yt2BiliError("请先完成或关闭扫码登录，再开始任务。")

    def dispatch(self, method, params):
        if not isinstance(params, dict):
            raise Yt2BiliError("请求参数必须为对象。")
        handlers = {
            "system.health": self.health, "system.diagnostics": self.diagnostics,
            "settings.get": lambda: self.config.public(), "settings.update": self.update_settings,
            "credentials.set": self.set_key, "credentials.test": self.test_key,
            "tasks.create": self.create, "tasks.list": self.list_tasks, "tasks.get": self.get_task,
            "tasks.retry": self.retry, "tasks.cancel": self.cancel, "tasks.submit": self.submit,
            "tasks.repair": self.repair, "tasks.update_metadata": self.update_metadata,
            "tasks.resolve": self.resolve, "tasks.cover": self.cover,
            "tasks.open_folder": self.open_folder,
            "auth.status": self.auth_status, "auth.login.start": self.login_start,
            "auth.login.cancel": self.login_cancel, "auth.renew": self.renew,
            "auth.import": self.import_cookies, "auth.youtube_export": self.export_youtube,
            "auth.clear": self.clear_auth,
            "files.read_urls": self.read_urls, "data.import": self.import_data,
            "logs.tail": self.log_tail, "logs.export": self.log_export,
        }
        handler = handlers.get(method)
        if handler is None:
            raise Yt2BiliError("不支持的方法：" + str(method))
        return handler(**params)

    def health(self):
        return {"protocol_version": 1, "version": "0.2.0-alpha.1", "queue": self.scheduler.snapshot(),
                "data_dir": str(self.paths.root)}

    def diagnostics(self):
        tools = []
        for name in ("ffmpeg", "ffprobe", "biliup", "deno", "node"):
            path = find_tool(name, self.paths.resources / "bin")
            found = shutil.which(path)
            version = "未找到"
            if found:
                try:
                    args = [path, "-version" if name.startswith("ff") else "--version"]
                    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                            timeout=10, **creation_options())
                    version = (result.stdout or result.stderr).splitlines()[0][:180]
                    found = found if result.returncode == 0 else None
                except (OSError, subprocess.TimeoutExpired, IndexError):
                    found = None
                    version = "无法运行"
            tools.append({"name": name, "path": path, "available": bool(found), "version": version})
        work = Path(self.config.values["work_dir"])
        work.mkdir(parents=True, exist_ok=True)
        return {"tools": tools, "free_bytes": shutil.disk_usage(work).free, "work_dir": str(work)}

    def update_settings(self, values):
        with self.mutation:
            self.ensure_idle()
            result = self.config.update(values)
            self.apply_environment()
            return result

    def set_key(self, value):
        with self.mutation:
            self.ensure_idle()
            self.config.set_key(value)
            return {"saved": True}

    def test_key(self):
        import deepl
        key = self.config.key()
        if not key:
            raise Yt2BiliError("请先保存 DeepL API 密钥。")
        try:
            usage = deepl.Translator(key).get_usage()
            return {"ok": True, "used": usage.character.count, "limit": usage.character.limit}
        except Exception as exc:
            raise Yt2BiliError("DeepL 检测失败，请检查密钥、配额或网络。") from exc

    def operation(self, operation_id, method, action):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", operation_id):
            raise Yt2BiliError("缺少有效操作 ID。")
        with self.mutation:
            previous = self.store.operation(operation_id, method)
            if previous is not None:
                return previous
            result = action()
            return self.store.operation(operation_id, method, result)

    def create(self, text, operation_id, mode="preview"):
        if mode not in ("preview", "auto"):
            raise Yt2BiliError("任务模式无效。")
        urls = parse_urls(text)
        def action():
            self.ensure_login_finished()
            if not self.config.key():
                raise Yt2BiliError("请先在账号与连接中配置 DeepL 密钥。")
            if mode == "auto" and not self.config.build().bili_cookies.is_file():
                raise Yt2BiliError("自动投稿前请先登录 B 站。")
            added, skipped = [], []
            for video_id, url in urls:
                old = self.store.get(video_id)
                if old:
                    skipped.append(video_id)
                    continue
                task = Task(video_id, url, "pending")
                self.scheduler.add(task, mode)
                added.append(video_id)
            return {"added": added, "skipped": skipped}
        return self.operation(operation_id, "tasks.create", action)

    def list_tasks(self, offset=0, limit=100, search="", status="", history=False):
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 200:
            raise Yt2BiliError("分页参数无效。")
        tasks = self.store.list_all()
        tasks = [item for item in tasks if (not search or search.lower() in (item.title_zh + item.title_orig + item.video_id).lower())
                 and (not status or item.status == status)
                 and (not history or item.status in ("submitted", "submission_unknown"))]
        all_tasks = self.store.list_all()
        return {"items": [{key: value for key, value in asdict(item).items() if key not in ("desc_orig", "desc_zh")} for item in tasks[offset:offset + limit]], "total": len(tasks),
                "counts": {name: sum(t.status == name for t in all_tasks) for name in ("downloading", "validating", "uploading", "ready", "submitted", "failed")},
                "queue": self.scheduler.snapshot()}

    def get_task(self, video_id):
        task = self.task(video_id)
        result = asdict(task)
        result["snapshot"] = self.store.get_job(video_id)
        result["file_exists"] = bool(task.video_path and Path(task.video_path).is_file())
        return result

    def retry(self, video_id, operation_id):
        def action():
            self.ensure_login_finished()
            task = self.task(video_id)
            if task.status not in ("failed", "cancelled", "interrupted"):
                raise Yt2BiliError("只有失败、取消或中断的任务可以继续；待核对投稿需先核对结果。")
            snapshot = self.store.get_job(video_id) or {}
            self.scheduler.add(task, "preview", snapshot.get("settings"))
            return {"queued": True}
        return self.operation(operation_id, "tasks.retry", action)

    def submit(self, video_id, operation_id):
        def action():
            self.ensure_login_finished()
            task = self.task(video_id)
            if task.status != "ready":
                raise Yt2BiliError("只有已准备好素材的任务可以确认投稿。")
            if not self.config.build().bili_cookies.is_file():
                raise Yt2BiliError("请先登录 B 站。")
            saved = self.store.get_job(video_id) or {}
            self.scheduler.add(task, "submit", saved.get("settings"), stage="validate")
            return {"queued": True}
        return self.operation(operation_id, "tasks.submit", action)

    def cancel(self, video_id):
        self.task(video_id)
        self.scheduler.cancel(video_id)
        return {"requested": True}

    def repair(self, video_id, operation_id):
        def action():
            self.ensure_login_finished()
            task = self.task(video_id)
            if task.status != "submitted" or not task.bv_id:
                raise Yt2BiliError("修复用于已取得 BV 号的稿件；只准备本地替换文件。")
            saved = self.store.get_job(video_id) or {}
            self.scheduler.add(task, "preview", saved.get("settings"), repair=True)
            return {"queued": True}
        return self.operation(operation_id, "tasks.repair", action)

    def update_metadata(self, video_id, title, description):
        with self.mutation:
            task = self.task(video_id)
            self.ensure_inactive(video_id)
            if task.status != "ready":
                raise Yt2BiliError("素材准备完成后才可编辑。")
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
                raise Yt2BiliError("标题须为 1～80 字。")
            if not isinstance(description, str) or len(description) > 2000:
                raise Yt2BiliError("简介不能超过 2000 字。")
            task.title_zh = title.strip().replace("\n", " ")
            body = description.split("\n\n————————\n原标题：")[0]
            task.desc_zh = translate.build_description(body, task.title_orig, task.uploader, task.url, 2000)
            self.store.upsert(task)
            for name, content in (("title.txt", task.title_zh), ("desc.txt", task.desc_zh)):
                (Path(task.work_dir) / name).write_text(content, encoding="utf-8")
            return asdict(task)

    def resolve(self, video_id, bv_id="", not_submitted=False):
        with self.mutation:
            task = self.task(video_id)
            self.ensure_inactive(video_id)
            if task.status != "submission_unknown":
                raise Yt2BiliError("此任务无需核对投稿结果。")
            if not_submitted is True:
                task.status = "interrupted"
                task.error = "用户已核对未提交，可手动继续准备素材。"
            else:
                if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bv_id):
                    raise Yt2BiliError("请填写核对后的完整 BV 号。")
                task.status, task.bv_id, task.error = "submitted", bv_id, "用户登记 BV 号；本地素材保留。"
            self.store.upsert(task)
            return asdict(task)

    def cover(self, video_id):
        task = self.task(video_id)
        if not task.cover_path or not task.work_dir:
            return {"image": None}
        file = Path(task.cover_path).resolve()
        if Path(task.work_dir).resolve() not in file.parents or not file.is_file():
            return {"image": None}
        with Image.open(file) as image:
            image.thumbnail((640, 360))
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, "JPEG", quality=80)
        return {"image": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()}

    def open_folder(self, video_id):
        task = self.task(video_id)
        path = Path(task.work_dir) if task.work_dir else None
        if not path or not path.is_dir():
            raise Yt2BiliError("素材目录已清理或不存在。")
        if os.name == "nt":
            os.startfile(str(path))
        else:
            import sys
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        return {"opened": True}

    def auth_status(self, verify=False):
        return {**account_status(self.paths.root / "secrets/bili_cookies.json", verify), "login": self.auth_state}

    def login_start(self):
        with self.mutation:
            self.ensure_idle()
            self.login_cancel()
            self.auth_state = {"status": "loading"}
            self.login = LoginSession(self.paths.root / "secrets/bili_cookies.json", self.emit)
            return self.login.start()

    def login_cancel(self):
        if self.login:
            self.login.cancel()
        self.auth_state = {"status": "idle"}
        return {"cancelled": True}

    def renew(self):
        with self.mutation:
            self.ensure_idle()
            bili_upload.renew(self.config.build())
            return self.auth_status(verify=True)

    def import_cookies(self, path, kind):
        with self.mutation:
            self.ensure_idle()
            source = Path(path)
            if not source.is_file() or source.stat().st_size > 5_000_000:
                raise Yt2BiliError("Cookie 文件不存在或过大。")
            content = source.read_text(encoding="utf-8-sig")
            if kind == "bilibili":
                self.login_cancel()
                atomic_json(self.paths.root / "secrets/bili_cookies.json", validate_login(json.loads(content)))
            elif kind == "youtube":
                import http.cookiejar
                jar = http.cookiejar.MozillaCookieJar(str(source))
                try:
                    jar.load(ignore_discard=True, ignore_expires=True)
                except Exception as exc:
                    raise Yt2BiliError("请使用 Netscape 格式的 YouTube Cookie 文件。") from exc
                target = self.paths.root / "secrets/youtube_cookies.txt"
                temporary = target.with_suffix(".tmp")
                temporary.write_text(content, encoding="utf-8")
                temporary.replace(target)
                if os.name != "nt":
                    target.chmod(0o600)
            else:
                raise Yt2BiliError("不支持的 Cookie 类型。")
            return {"imported": True}

    def export_youtube(self, browser="edge"):
        with self.mutation:
            self.ensure_idle()
            if browser not in ("edge", "chrome", "firefox"):
                raise Yt2BiliError("不支持的浏览器。")
            from dataclasses import replace
            settings = replace(self.config.build(), youtube_cookies=self.paths.root / "secrets/youtube_cookies.txt")
            youtube.export_browser_cookies(settings, browser)
            return {"exported": True}

    def clear_auth(self, kind):
        with self.mutation:
            self.ensure_idle()
            names = {"bilibili": "bili_cookies.json", "youtube": "youtube_cookies.txt"}
            if kind not in names:
                raise Yt2BiliError("账号类型无效。")
            self.login_cancel()
            (self.paths.root / "secrets" / names[kind]).unlink(missing_ok=True)
            return {"cleared": True}

    def read_urls(self, path):
        file = Path(path)
        if file.suffix.lower() != ".txt" or file.stat().st_size > 250_000:
            raise Yt2BiliError("请选择小于 250 KB 的 TXT 文件。")
        text = file.read_text(encoding="utf-8-sig")
        parse_urls(text)
        return {"text": text}

    def import_data(self, path):
        with self.mutation:
            self.ensure_idle()
            source = Path(path).resolve()
            db = source / "data/tasks.sqlite"
            if not db.is_file():
                raise Yt2BiliError("所选目录没有 data/tasks.sqlite。")
            backup = self.paths.root / "data/import-backup.sqlite"
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as old, closing(sqlite3.connect(backup)) as target:
                old.backup(target)
                old.row_factory = sqlite3.Row
                rows = old.execute("SELECT * FROM tasks").fetchall()
            imported = 0
            for row in rows:
                if not VIDEO_ID.fullmatch(row["video_id"]) or self.store.get(row["video_id"]):
                    continue
                task = Task(**{name: row[name] for name in Task.__dataclass_fields__})
                if task.work_dir:
                    expected = (source / "work" / task.video_id).resolve()
                    if Path(task.work_dir).resolve() != expected:
                        task.work_dir = task.video_path = task.cover_path = ""
                    else:
                        for attr in ("video_path", "cover_path"):
                            if getattr(task, attr) and expected not in Path(getattr(task, attr)).resolve().parents:
                                setattr(task, attr, "")
                if task.status == "uploading" or (task.status == "submitted" and not task.bv_id):
                    task.status = "submission_unknown"
                elif task.status not in ("submitted", "ready", "failed", "cancelled", "submission_unknown"):
                    task.status = "interrupted"
                self.store.upsert(task)
                imported += 1
            return {"imported": imported, "note": "只导入任务记录，素材保留原位，账号和密钥需单独配置。"}

    def log_tail(self, video_id=None):
        with self.logs_lock:
            return {"items": [entry for entry in self.logs if not video_id or entry.get("video_id") == video_id][-300:]}

    def log_export(self, path):
        target = Path(path)
        if target.suffix.lower() not in (".txt", ".log"):
            raise Yt2BiliError("日志请保存为 .txt 或 .log。")
        from yt2bili.desktop_worker import redact
        parts = []
        for index in (3, 2, 1, 0):
            file = self.paths.root / "logs" / (f"desktop.log.{index}" if index else "desktop.log")
            if file.is_file():
                parts.append("\n".join(redact(line) for line in file.read_text(encoding="utf-8", errors="replace").splitlines()))
        if parts:
            text = "\n".join(parts)
        else:
            with self.logs_lock:
                text = "\n".join(f"{item['time']} {item['level']} {redact(item['message'])}" for item in self.logs)
        target.write_text(text, encoding="utf-8")
        return {"exported": True}

    def close(self):
        self.login_cancel()
        self.scheduler.close()
        self.store.close()

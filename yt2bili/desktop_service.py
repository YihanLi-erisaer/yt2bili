from __future__ import annotations

import base64
import io
import json
import hashlib
import uuid
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

from yt2bili import bili_upload, youtube, translate, publications
from yt2bili.db import Task, TaskStore
from yt2bili.desktop_auth import LoginSession, account_status, validate_login
from yt2bili.desktop_settings import DesktopSettings, atomic_json
from yt2bili.exceptions import Yt2BiliError
from yt2bili.process_manager import creation_options
from yt2bili.scheduler import Scheduler
from yt2bili.accounts import AccountService
from yt2bili.identity import VIDEO_ID, parse_single_video_url
from yt2bili.locking import FileLock, account_guard, coordination_dir, work_lock
from dataclasses import replace
from yt2bili.tools import find_tool


def parse_urls(text):
    # Internal compatibility helper; all public input is strictly one URL.
    return [parse_single_video_url(text)]

class DesktopService:
    def __init__(self, paths, emit, vault=None, config=None):
        self.paths, self.output = paths, emit
        self.logs = deque(maxlen=1500)
        self.logs_lock = threading.Lock()
        self.mutation = threading.RLock()
        self.login = None
        self.auth_state = {"status": "idle"}
        self.config = config or DesktopSettings(paths, vault)
        self.owner_lock = FileLock(paths.root / "execution.lock")
        self.owner_lock.__enter__()
        try:
            self.store = TaskStore(paths.root / "data/tasks.sqlite", self.emit)
            self.accounts = AccountService(paths.root, self.store, self.emit)
            from yt2bili.douyin import DouyinService
            from yt2bili.acfun import AcfunService
            self.douyin = DouyinService(self.store, self.config, self.emit)
            self.acfun = AcfunService(self.store, self.config, self.emit)
            self.scheduler = Scheduler(self.store, self.config, self.emit, self.accounts, self.douyin, self.acfun)
        except BaseException:
            self.owner_lock.__exit__(None, None, None)
            raise
        from yt2bili.translation.jobs import TranslationJobs
        self.translation_jobs = TranslationJobs(paths.root / "translation", self.emit)
        self.migration_notes = []
        legacy_cookie = paths.root / "secrets/bili_cookies.json"
        if not self.store.accounts(True) and legacy_cookie.is_file():
            try:
                if legacy_cookie.stat().st_size > 5_000_000:
                    raise ValueError("oversize")
                self.accounts.bind(json.loads(legacy_cookie.read_text(encoding="utf-8-sig")), verify=False)
                self.migration_notes.append("旧登录文件已导入为待验证账号；历史任务仍需确认归属。")
            except (ValueError, OSError, Yt2BiliError):
                self.migration_notes.append("旧登录文件无法确认 UID，已保留原文件，请重新登录。")
        self._closed = False
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
        if event == "accounts.changed" and hasattr(self, "scheduler"):
            self.scheduler.sync_accounts(payload.get("account_id"))
        if event == "douyin.auth.changed" and hasattr(self, "scheduler"):
            self.scheduler.douyin_lane.wake(credentials=True)
        if event == "acfun.auth.changed" and hasattr(self, "scheduler"):
            self.scheduler.acfun_lane.wake(credentials=True)
        if event == "acfun.queue.resume" and hasattr(self, "scheduler"):
            lane = self.scheduler.acfun_lane
            with lane.condition:
                for item in lane.items:
                    item.wait_reason, item.deadline = "", None
                lane.condition.notify_all()
        if event == "douyin.queue.resume" and hasattr(self, "scheduler"):
            lane = self.scheduler.douyin_lane
            with lane.condition:
                for item in lane.items:
                    if item.wait_reason == "rate_limited": item.wait_reason, item.deadline = "", None
                lane.condition.notify_all()
        self.output(event, payload)

    def add_log(self, entry):
        with self.logs_lock:
            self.logs.append(entry)
        self.output("task.log", entry)

    def ensure_idle(self):
        if self.scheduler.snapshot()["active"] or self.translation_jobs.active():
            raise Yt2BiliError("请等待当前任务结束后修改设置或账号。")

    def task(self, task_id):
        if not isinstance(task_id, str) or len(task_id) > 100:
            raise Yt2BiliError("任务 ID 无效。")
        return self.store.require(task_id)

    def ensure_inactive(self, task_id):
        task = self.task(task_id)
        job = self.store.get_job(task.task_id) or {}
        if job.get("owner_session_id") == self.scheduler.session_id and job.get("execution_state") in ("queued", "running", "waiting"):
            raise Yt2BiliError("任务正在执行，请先等待或取消。")

    def dispatch(self, method, params):
        if not isinstance(params, dict):
            raise Yt2BiliError("请求参数必须为对象。")
        handlers = {
            "system.health": self.health, "system.diagnostics": self.diagnostics,
            "settings.get": lambda: self.config.public(), "settings.update": self.update_settings,
            "translation.status": self.translation_status, "translation.test": self.translation_test,
            "translation.install": self.translation_install, "translation.jobs.get": self.translation_jobs.get,
            "translation.jobs.cancel": self.translation_jobs.cancel, "tasks.retranslate": self.retranslate,
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
            "data.import": self.import_data,
            "accounts.list": lambda: {"items": self.accounts.list(True), "limit": 5},
            "accounts.add": self.login_start, "accounts.rename": self.accounts.rename,
            "accounts.archive": self.archive_account, "accounts.resume_uploads": self.resume_uploads,
            "accounts.verify": self.accounts.verify,
            "tasks.bind_legacy_account": self.bind_legacy,
            "douyin.auth.status": self.douyin.status, "douyin.auth.start": self.douyin.start,
            "douyin.auth.configure": self.configure_douyin,
            "douyin.auth.cancel": lambda: self.douyin.request("POST", "/v1/auth/cancel"),
            "douyin.auth.clear": self.douyin.clear, "douyin.accounts.archive": self.douyin.archive,
            "douyin.accounts.resume_uploads": self.douyin.resume,
            "acfun.auth.status": self.acfun.status, "acfun.auth.start": self.acfun.start,
            "acfun.auth.poll": self.acfun.poll, "acfun.auth.cancel": self.acfun.cancel,
            "acfun.auth.clear": self.acfun.clear, "acfun.accounts.archive": self.acfun.archive,
            "acfun.accounts.resume_uploads": self.acfun.resume,
            "publications.update_metadata": self.update_publication,
            "publications.retry": self.retry_publication, "publications.abandon": self.abandon_publication,
            "publications.resolve": self.resolve_publication, "publications.cancel": self.cancel_publication,
            "system.prepare_shutdown": self.prepare_shutdown,
            "system.shutdown_status": self.shutdown_status,
            "logs.tail": self.log_tail, "logs.export": self.log_export,
        }
        handler = handlers.get(method)
        if handler is None:
            raise Yt2BiliError("不支持的方法：" + str(method))
        return handler(**params)

    def health(self):
        capabilities = ["single_url", "account_lanes", "graceful_shutdown", "local_translation_v1", "douyin_sync_v1"]
        if self.config.values.get("acfun_experimental_enabled"):
            capabilities.append("acfun_sync_v1")
        return {"protocol_version": 2, "schema_version": 5, "account_limit": 5,
                "capabilities": capabilities,
                "migration_notes": self.migration_notes, "version": "0.2.0-alpha.1", "queue": self.scheduler.snapshot(),
                "data_dir": str(self.paths.root)}

    def translation_status(self):
        from yt2bili.translation.runtime import status, manifest
        return {"local": status(self.config.values, self.paths.root / "translation"),
                "primary": self.config.values["translation_primary"],
                "fallback_enabled": self.config.values["translation_fallback_enabled"],
                "model": manifest()["model"], "runtime": manifest()["runtime"]}

    def translation_install(self, operation_id, model_id="qwen3:8b", offline_path=None):
        if model_id != "qwen3:8b":
            raise Yt2BiliError("不支持的模型。")
        from yt2bili.translation.deployment import install
        with self.mutation:
            if self.scheduler.snapshot()["active"]:
                raise Yt2BiliError("请等待视频任务结束后安装组件。")
            config = dict(self.config.values)
            return self.translation_jobs.start("install", operation_id,
                lambda: install(config, self.paths.root / "translation", offline_path=offline_path))

    def translation_test(self, provider, operation_id):
        if provider not in ("local_llm", "deepl"):
            raise Yt2BiliError("不支持的翻译服务。")
        from dataclasses import replace
        from yt2bili.translation.service import translate as translate_group
        with self.mutation:
            if self.scheduler.snapshot()["active"]:
                raise Yt2BiliError("请等待视频任务结束后试译。")
            settings = replace(self.config.build(), translation_primary=provider, translation_fallback_enabled=False)
            return self.translation_jobs.start("test:" + provider, operation_id,
                lambda: asdict(translate_group(settings, "A better workflow", "Build useful tools. Keep version 2.0.", "en", 80, 1000)))

    def retranslate(self, task_id, operation_id, replace_edited=False):
        def action():
            self.ensure_inactive(task_id)
            task = self.task(task_id)
            if task.status != "ready":
                raise Yt2BiliError("只能重新翻译尚未投稿且已准备好的任务。")
            record = self.store.translation(task_id) or {}
            if record.get("user_edited") and replace_edited is not True:
                raise Yt2BiliError("重新翻译将替换已编辑内容，请确认后继续。")
            saved = (self.store.get_job(task_id) or {}).get("settings", self.config.snapshot())
            from yt2bili.translation.config import DEFAULTS
            saved = {**saved, **{k: self.config.values[k] for k in DEFAULTS}}
            self.scheduler.add(task, "retranslate", saved, stage="upload")
            return {"queued": True}
        return self.operation(operation_id, "tasks.retranslate", action, {"task_id": task_id, "replace_edited": replace_edited})

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
        if not isinstance(values, dict): raise Yt2BiliError("设置参数必须为对象。")
        with self.mutation:
            self.ensure_idle()
            if values.get("douyin_broker_url", self.config.values["douyin_broker_url"]) != self.config.values["douyin_broker_url"] and self.douyin.account():
                raise Yt2BiliError("更换抖音授权服务前须先归档抖音账号。")
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

    def operation(self, operation_id, method, action, params=None, preflight=None):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", operation_id):
            raise Yt2BiliError("缺少有效操作 ID。")
        fingerprint = hashlib.sha256(json.dumps(params or {}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.mutation:
            previous = self.store.operation(operation_id, method, request_hash=fingerprint)
            if previous is not None:
                return previous
            if self.scheduler.closing:
                raise Yt2BiliError("应用正在退出。")
            if preflight:
                preflight()
            with self.store.transaction():
                previous = self.store.operation(operation_id, method, request_hash=fingerprint)
                if previous is not None:
                    return previous
                result = action()
                self.store.operation(operation_id, method, result, request_hash=fingerprint)
            self.scheduler.signal.set()
            return result

    def create(self, url, operation_id, account_id=None, mode="preview", sync_douyin=False, douyin_account_id=None, douyin_binding_revision=None,
               sync_acfun=False, acfun_account_id=None, acfun_binding_revision=None):
        if type(sync_douyin) is not bool: raise Yt2BiliError("同步抖音必须为开关值。")
        if type(sync_acfun) is not bool: raise Yt2BiliError("同步 AcFun 必须为开关值。")
        dy_account = ac_account = None
        video_id, canonical = parse_single_video_url(url)
        if mode not in ("preview", "auto"):
            raise Yt2BiliError("任务模式无效。")
        if not account_id or not isinstance(account_id, str):
            raise Yt2BiliError("请选择本次投稿的 Bilibili 账号。")
        def preflight():
            nonlocal dy_account, ac_account
            if sync_douyin:
                dy_account = self.douyin.check(douyin_account_id, douyin_binding_revision, auto=mode == "auto")
            if sync_acfun:
                ac_account = self.acfun.check(acfun_account_id, acfun_binding_revision, auto=mode == "auto")
            account = self.store.account(account_id)
            if self.translation_jobs.active():
                raise Yt2BiliError("请等待翻译组件操作结束后创建任务。")
            if mode == "auto" and account["auth_state"] != "valid":
                self.accounts.verify(account_id)
        def action():
            account = self.store.account(account_id)
            old = self.store.for_account(video_id, account_id)
            if old:
                selected = {"bilibili"} | ({"douyin"} if sync_douyin else set()) | ({"acfun"} if sync_acfun else set())
                existing = {p["platform"] for p in publications.items(self.store, old.task_id)}
                if selected != existing:
                    raise Yt2BiliError("此 Bilibili 账号已有同视频任务，目标不可追加；请继续原任务。")
                return {"task_id": old.task_id, "account_id": account_id, "created": False, "status": old.status}
            task = Task(video_id, canonical, "pending", task_id=str(uuid.uuid4()), account_id=account_id,
                        account_uid_snapshot=account["uid"], account_name_snapshot=account["nickname"] or account["uid"])
            self.store.upsert(task)
            publications.ensure_bili(self.store, task)
            if sync_douyin:
                account_now = self.douyin.account()
                if not account_now or account_now != dy_account:
                    raise Yt2BiliError("抖音账号状态已变化，请重试。")
                duplicate = self.store._conn.execute("SELECT task_id FROM task_publications WHERE platform='douyin' AND account_id=? AND source_video_id=?", (dy_account["account_id"], video_id)).fetchone()
                if duplicate: raise Yt2BiliError("此抖音账号已有同视频任务：" + duplicate[0] + "；请取消同步或继续原任务。")
                self.store._conn.execute("INSERT INTO task_publications(publication_id,task_id,platform,account_id,source_video_id) VALUES(?,?,'douyin',?,?)", (str(uuid.uuid4()), task.task_id, dy_account["account_id"], video_id))
                dy = publications.for_platform(self.store, task.task_id, "douyin")
                publications.change(self.store, dy["publication_id"], snapshot=json.dumps({k: dy_account[k] for k in ("client_key", "open_id", "nickname", "binding_revision")}))
            if sync_acfun:
                account_now = self.acfun.account()
                if not account_now or account_now != ac_account:
                    raise Yt2BiliError("AcFun 账号状态已变化，请重试。")
                duplicate = self.store._conn.execute("SELECT task_id FROM task_publications WHERE platform='acfun' AND account_id=? AND source_video_id=?", (ac_account["account_id"], video_id)).fetchone()
                if duplicate: raise Yt2BiliError("此 AcFun 账号已有同视频任务：" + duplicate[0] + "；请继续原任务。")
                self.store._conn.execute("INSERT INTO task_publications(publication_id,task_id,platform,account_id,source_video_id) VALUES(?,?,'acfun',?,?)", (str(uuid.uuid4()), task.task_id, ac_account["account_id"], video_id))
                ac = publications.for_platform(self.store, task.task_id, "acfun")
                publications.change(self.store, ac["publication_id"], snapshot=json.dumps({"user_id": ac_account["user_id"], "nickname": ac_account["nickname"],
                    "binding_revision": ac_account["binding_revision"], "adapter_version": ac_account["adapter_version"], "channel_id": 0, "tags": ["转载"]}, ensure_ascii=False))
            self.scheduler.add(task, mode)
            return {"task_id": task.task_id, "account_id": account_id, "created": True, "status": task.status}
        params = {"url": canonical, "account_id": account_id, "mode": mode}
        if sync_douyin: params.update(sync_douyin=True, douyin_account_id=douyin_account_id, douyin_binding_revision=douyin_binding_revision)
        if sync_acfun: params.update(sync_acfun=True, acfun_account_id=acfun_account_id, acfun_binding_revision=acfun_binding_revision)
        return self.operation(operation_id, "tasks.create", action, params, preflight)

    def bind_legacy(self, task_id, account_id):
        with self.mutation, self.store.transaction():
            task = self.task(task_id)
            self.ensure_inactive(task.task_id)
            if task.account_id:
                raise Yt2BiliError("已绑定账号的任务不能改投。")
            account = self.store.account(account_id, active=task.status != "submitted")
            if self.store.for_account(task.video_id, account_id):
                raise Yt2BiliError("该账号已存在同视频任务，请保留并核对历史记录。")
            task.account_id, task.account_uid_snapshot = account_id, account["uid"]
            task.account_name_snapshot = account["nickname"] or account["uid"]
            self.store.upsert(task)
            self.store._conn.execute("UPDATE task_publications SET account_id=? WHERE task_id=? AND platform='bilibili' AND account_id IS NULL", (account_id, task.task_id))
            return asdict(task)

    def list_tasks(self, offset=0, limit=100, search="", status="", history=False, account_id=""):
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 200:
            raise Yt2BiliError("分页参数无效。")
        tasks = self.store.list_all()
        tasks = [item for item in tasks if (not search or search.lower() in (item.title_zh + item.title_orig + item.video_id).lower())
                 and (not account_id or item.account_id == account_id) and (not status or item.status == status)
                 and (not history or item.status in ("submitted", "submission_unknown", "partial_success", "completed_with_abandon"))]
        all_tasks = self.store.list_all()
        return {"items": [{**{key: value for key, value in asdict(item).items() if key not in ("desc_orig", "desc_zh")},
                           "publications": publications.items(self.store, item.task_id),
                           "run_id": (self.store.get_job(item.task_id) or {}).get("run_id")} for item in tasks[offset:offset + limit]], "total": len(tasks),
                "counts": {name: sum(t.status == name for t in all_tasks) for name in ("downloading", "validating", "uploading", "ready", "submitted", "failed")},
                "queue": self.scheduler.snapshot(), "all_total": len(all_tasks)}

    def get_task(self, task_id):
        task = self.task(task_id)
        result = asdict(task)
        result["publications"] = publications.items(self.store, task_id)
        result["translation"] = self.store.translation(task_id)
        result["snapshot"] = self.store.get_job(task_id)
        result["run_id"] = (result["snapshot"] or {}).get("run_id")
        result["file_exists"] = bool(task.video_path and Path(task.video_path).is_file())
        return result

    def retry(self, task_id, operation_id, use_current_translation_settings=False):
        def action():
            task = self.task(task_id)
            if any(p["status"] in publications.TERMINAL | {"submission_unknown"} for p in publications.items(self.store, task_id)):
                raise Yt2BiliError("请在平台投稿记录中单独继续未完成目标。")
            if task.status not in ("failed", "cancelled", "interrupted"):
                raise Yt2BiliError("只有失败、取消或中断的任务可以继续；待核对投稿需先核对结果。")
            snapshot = self.store.get_job(task_id) or {}
            settings = snapshot.get("settings")
            if use_current_translation_settings is True:
                from yt2bili.translation.config import DEFAULTS
                settings = {**(settings or self.config.snapshot()), **{k: self.config.values[k] for k in DEFAULTS}}
                record = self.store.translation(task_id) or {}
                record["config_snapshot"] = {k: self.config.values[k] for k in DEFAULTS}
                self.store.save_translation(task, record)
            self.scheduler.add(task, "preview", settings)
            return {"queued": True}
        return self.operation(operation_id, "tasks.retry", action, {"task_id": task_id, "use_current_translation_settings": use_current_translation_settings})

    def submit(self, task_id, operation_id, targets=None, revisions=None):
        if targets is not None and (not isinstance(targets, list) or not targets or len(targets) > 3 or any(not isinstance(v, str) for v in targets) or len(set(targets)) != len(targets)):
            raise Yt2BiliError("投稿目标列表无效。")
        if revisions is not None and not isinstance(revisions, dict): raise Yt2BiliError("投稿版本快照无效。")
        def action():
            task = self.task(task_id)
            self.ensure_inactive(task_id)
            if task.status not in ("ready", "partial_success"):
                raise Yt2BiliError("只有已准备好素材的任务可以确认投稿。")
            saved = self.store.get_job(task_id) or {}
            pubs = publications.items(self.store, task_id)
            if targets is not None and set(targets) - {p["publication_id"] for p in pubs}: raise Yt2BiliError("投稿目标不属于此任务。")
            selected = [p for p in pubs if targets is None and p["status"] == "ready" or targets is not None and p["publication_id"] in targets]
            self.store.account(task.account_id, active=any(p["platform"] == "bilibili" for p in selected))
            if not selected or any(p["status"] != "ready" for p in selected): raise Yt2BiliError("投稿目标尚未准备好。")
            if revisions is not None and any(revisions.get(p["publication_id"]) != p["revision"] for p in selected): raise Yt2BiliError("文案已变化，请刷新预览后确认。")
            for p in selected:
                if p["platform"] == "acfun":
                    self.acfun.check(p["account_id"])
                    self.acfun.validate_assets(type("TaskRef", (), {"task_id": task_id})(), json.loads(p["snapshot"]))
            publications.freeze(self.store, task_id, [p["publication_id"] for p in selected])
            self.scheduler.add(task, "submit", saved.get("settings"), stage="validate", targets=[p["publication_id"] for p in selected])
            return {"queued": True}
        params = {"task_id": task_id}
        if targets is not None: params.update(targets=targets, revisions=revisions)
        return self.operation(operation_id, "tasks.submit", action, params)

    def configure_douyin(self, url, pairing_key):
        with self.mutation:
            self.ensure_idle()
            return self.douyin.configure(url, pairing_key)

    def update_publication(self, publication_id, text=None, revision=None, title=None, description=None, channel_id=None, tags=None):
        with self.mutation, self.store.transaction():
            p = publications.get(self.store, publication_id)
            self.ensure_inactive(p["task_id"])
            if p["status"] != "ready" or p["revision"] != revision:
                raise Yt2BiliError("只能编辑预览中的投稿目标，请刷新后重试。")
            if p["platform"] == "douyin":
                if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000: raise Yt2BiliError("抖音文案须为 1～1000 字。")
                publications.change(self.store, publication_id, text=text.strip())
            elif p["platform"] == "acfun":
                if not isinstance(title, str) or not 1 <= len(title.strip()) <= 50: raise Yt2BiliError("AcFun 标题须为 1～50 字。")
                if not isinstance(description, str) or len(description) > 1000: raise Yt2BiliError("AcFun 简介不能超过 1000 字。")
                if type(channel_id) is not int or channel_id <= 0: raise Yt2BiliError("请选择有效 AcFun 分区 ID。")
                if not isinstance(tags, list) or len(tags) > 6 or any(not isinstance(t, str) or not t.strip() or len(t) > 30 for t in tags):
                    raise Yt2BiliError("AcFun 标签最多 6 个，每个不超过 30 字。")
                snapshot = json.loads(p["snapshot"])
                snapshot.update(title=title.strip(), description=description, channel_id=channel_id, tags=[t.strip() for t in tags])
                publications.change(self.store, publication_id, snapshot=json.dumps(snapshot, ensure_ascii=False))
            else: raise Yt2BiliError("此平台不支持编辑投稿目标。")
            return publications.get(self.store, publication_id)

    def retry_publication(self, publication_id, operation_id):
        def action():
            p = publications.get(self.store, publication_id)
            self.ensure_inactive(p["task_id"])
            if p["status"] not in ("failed", "cancelled", "interrupted", "blocked_validation"):
                raise Yt2BiliError("只能继续已失败、取消或中断的目标；结果待核对时禁止重投。")
            if p["platform"] == "acfun" and self.store._conn.execute("SELECT 1 FROM acfun_attempts WHERE publication_id=? AND create_intent_at!='' AND phase!='resolved_not_submitted'", (publication_id,)).fetchone():
                raise Yt2BiliError("AcFun 已记录创建作品意图，须先核对；禁止重投。")
            task = self.task(p["task_id"])
            if not task.video_path or not Path(task.video_path).is_file(): raise Yt2BiliError("本地素材缺失，请先恢复素材；不会自动重发已成功目标。")
            publications.change(self.store, publication_id, status="ready", error="")
            publications.project(self.store, task.task_id)
            return {"ready": True}
        return self.operation(operation_id, "publications.retry", action, {"publication_id": publication_id})

    def cancel_publication(self, publication_id):
        with self.mutation, self.scheduler.guard:
            p = publications.get(self.store, publication_id)
            if p["status"] in publications.INFLIGHT | publications.TERMINAL | {"submission_unknown"}:
                raise Yt2BiliError("此目标不能取消，请等待或核对结果。")
            root = self.scheduler.active.get(p["task_id"])
            if root:
                child = root.children.get(p["platform"])
                if not child: raise Yt2BiliError("共享素材准备阶段请取消整个任务。")
                child.cancel.set()
            publications.change(self.store, publication_id, status="cancelled", error="用户取消，素材保留。")
            publications.project(self.store, p["task_id"])
            return {"cancelled": True}

    def abandon_publication(self, publication_id):
        with self.mutation, self.store.transaction():
            p = publications.get(self.store, publication_id)
            self.ensure_inactive(p["task_id"])
            if p["status"] in publications.INFLIGHT | {"submitted", "submission_unknown"}: raise Yt2BiliError("请先核对投稿结果；已提交目标不能放弃。")
            publications.change(self.store, publication_id, status="abandoned", error="用户明确放弃该目标。")
            publications.project(self.store, p["task_id"])
        with work_lock(Path(self.task(p["task_id"]).work_dir)):
            self.scheduler.uploader.cleanup(p["task_id"])
        return {"abandoned": True}

    def resolve_publication(self, publication_id, remote_id="", not_submitted=False):
        if type(not_submitted) is not bool: raise Yt2BiliError("核对结果必须为明确的开关值。")
        with self.mutation, self.store.transaction():
            p = publications.get(self.store, publication_id)
            self.ensure_inactive(p["task_id"])
            if p["status"] != "submission_unknown": raise Yt2BiliError("此目标无需核对。")
            if not_submitted is not True and (not isinstance(remote_id, str) or not remote_id.strip() or len(remote_id) > 512): raise Yt2BiliError("请填写已核对的作品 ID。")
            if p["platform"] == "bilibili" and not not_submitted and not re.fullmatch(r"BV[0-9A-Za-z]{10}", remote_id): raise Yt2BiliError("BV 号格式无效。")
            if p["platform"] == "acfun" and not not_submitted and not re.fullmatch(r"(?i:AC)[1-9][0-9]*", remote_id): raise Yt2BiliError("AC 号格式无效。")
            if p["platform"] == "douyin":
                self.douyin.request("POST", "/v1/publications/" + publication_id + "/resolve", {"not_submitted": not_submitted, "remote_id": remote_id})
            if p["platform"] == "acfun":
                self.store._conn.execute("UPDATE acfun_attempts SET phase=?,douga_id=? WHERE publication_id=? AND create_intent_at!=''",
                    ("resolved_not_submitted" if not_submitted else "manually_submitted", "" if not_submitted else remote_id.removeprefix("AC"), publication_id))
            publications.change(self.store, publication_id, status="interrupted" if not_submitted else "submitted", remote_id="" if not_submitted else remote_id, retain_assets=1, error="用户核对结果；保留本地素材。")
            if p["platform"] == "bilibili" and not not_submitted: self.store.update(p["task_id"], bv_id=remote_id)
            publications.project(self.store, p["task_id"])
            return self.get_task(p["task_id"])

    def cancel(self, task_id):
        self.task(task_id)
        self.scheduler.cancel(task_id)
        return {"requested": True}

    def repair(self, task_id, operation_id):
        def action():
            task = self.task(task_id)
            if task.status != "submitted" or not task.bv_id:
                raise Yt2BiliError("修复用于已取得 BV 号的稿件；只准备本地替换文件。")
            saved = self.store.get_job(task_id) or {}
            self.scheduler.add(task, "preview", saved.get("settings"), repair=True)
            return {"queued": True}
        return self.operation(operation_id, "tasks.repair", action, {"task_id": task_id})

    def update_metadata(self, task_id, title, description):
        with self.mutation:
            task = self.task(task_id)
            self.ensure_inactive(task_id)
            if task.status != "ready":
                raise Yt2BiliError("素材准备完成后才可编辑。")
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
                raise Yt2BiliError("标题须为 1～80 字。")
            if not isinstance(description, str) or len(description) > 2000:
                raise Yt2BiliError("简介不能超过 2000 字。")
            task.metadata_revision += 1
            task.title_zh = title.strip().replace("\n", " ")
            body = description.split("\n\n————————\n原标题：")[0]
            task.desc_zh = translate.build_description(body, task.title_orig, task.uploader, task.url, 2000)
            record = self.store.translation(task_id) or {}
            record.update(state="edited", user_edited=True, description=body)
            self.store.save_translation(task, record)
            p = publications.for_platform(self.store, task.task_id, "bilibili")
            if p: publications.change(self.store, p["publication_id"], text=task.title_zh)
            for name, content in (("title.txt", task.title_zh), ("desc.txt", task.desc_zh)):
                (Path(task.work_dir) / name).write_text(content, encoding="utf-8")
            return asdict(task)

    def resolve(self, task_id, bv_id="", not_submitted=False):
        with self.mutation:
            task = self.task(task_id)
            if publications.dual(self.store, task_id): raise Yt2BiliError("请在对应平台记录中分别核对结果。")
            self.ensure_inactive(task_id)
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
            p = publications.for_platform(self.store, task.task_id, "bilibili")
            if p: publications.change(self.store, p["publication_id"], status=task.status, remote_id=task.bv_id, retain_assets=1)
            return asdict(task)

    def cover(self, task_id):
        task = self.task(task_id)
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

    def open_folder(self, task_id):
        task = self.task(task_id)
        path = Path(task.work_dir) if task.work_dir else None
        if not path or not path.is_dir():
            raise Yt2BiliError("素材目录已清理或不存在。")
        if os.name == "nt":
            os.startfile(str(path))
        else:
            import sys
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        return {"opened": True}

    def auth_status(self, verify=False, account_id=None):
        if verify and account_id:
            self.accounts.verify(account_id)
        items = self.accounts.list()
        return {"accounts": items, "limit": 5, "configured": bool(items),
                "login": self.auth_state}

    def login_start(self, account_id=None):
        if self.scheduler.closing:
            raise Yt2BiliError("应用正在退出。")
        if account_id:
            self.store.account(account_id)
        elif len(self.accounts.list()) >= 5:
            raise Yt2BiliError("最多支持 5 个 Bilibili 账号。")
        self.login_cancel()
        self.auth_state = {"status": "loading", "account_id": account_id}
        self.login = LoginSession(None, self.emit, account_id=account_id,
                                  commit=lambda info: self.accounts.bind(info, account_id))
        return self.login.start()

    def login_cancel(self, session_id=None, account_id=None):
        if self.login and (not session_id or self.login.session_id == session_id):
            self.login.cancel()
            self.auth_state = {"status": "idle"}
        return {"cancelled": True}

    def renew(self, account_id):
        account = self.store.account(account_id)
        with account_guard(account["uid"]):
            account, info = self.accounts.read(account_id)
            temporary = self.paths.root / "secrets/bilibili" / account_id / ("renew-" + str(uuid.uuid4()) + ".json")
            try:
                atomic_json(temporary, info)
                bili_upload.renew(replace(self.config.build(), bili_cookies=temporary))
                self.accounts.bind(json.loads(temporary.read_text(encoding="utf-8")), account_id, locked=True)
            finally:
                temporary.unlink(missing_ok=True)
        return self.auth_status()

    def import_cookies(self, path, kind, account_id=None):
        source = Path(path)
        if not source.is_file() or source.stat().st_size > 5_000_000:
            raise Yt2BiliError("Cookie 文件不存在或过大。")
        content = source.read_text(encoding="utf-8-sig")
        if kind == "bilibili":
            result = self.accounts.bind(validate_login(json.loads(content)), account_id)
            return {"imported": True, "account_id": result["account_id"]}
        if kind != "youtube":
            raise Yt2BiliError("不支持的 Cookie 类型。")
        with self.mutation:
            self.ensure_idle()
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
            return {"imported": True}

    def archive_account(self, account_id):
        with self.mutation:
            return self.accounts.archive(account_id)

    def resume_uploads(self, account_id, confirmed_no_upload=False):
        account = self.store.account(account_id)
        if confirmed_no_upload is not True:
            raise Yt2BiliError("请先核对没有仍在运行的投稿进程及该账号创作中心。")
        with account_guard(account["uid"]):
            marker = coordination_dir(account["uid"]) / "attempt-state.json"
            from yt2bili.process_manager import process_alive
            try:
                state = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
            except (OSError, ValueError):
                state = {}
            if state.get("inflight") and (process_alive(state.get("child_pid")) or process_alive(state.get("owner_pid"))):
                raise Yt2BiliError("检测到原投稿进程仍可能运行，不能恢复；请先结束原实例并核对。")
            # Explicit user confirmation only; unknown tasks remain frozen.
            import time
            atomic_json(marker, {"ended": time.time()})
        self.scheduler.sync_accounts(account_id)
        return {"resumed": True}

    def prepare_shutdown(self):
        self.login_cancel()
        with self.translation_jobs.lock:
            self.translation_jobs.closing = True
            for cancel, _ in self.translation_jobs.running.values():
                cancel.set()
        self.scheduler.prepare_shutdown()
        return self.shutdown_status()

    def shutdown_status(self):
        result = self.scheduler.shutdown_status()
        result["ready"] = result["ready"] and not self.translation_jobs.active()
        return result

    def export_youtube(self, browser="edge"):
        with self.mutation:
            self.ensure_idle()
            if browser not in ("edge", "chrome", "firefox"):
                raise Yt2BiliError("不支持的浏览器。")
            from dataclasses import replace
            settings = replace(self.config.build(), youtube_cookies=self.paths.root / "secrets/youtube_cookies.txt")
            youtube.export_browser_cookies(settings, browser)
            return {"exported": True}

    def clear_auth(self, kind, account_id=None):
        if kind == "bilibili":
            return self.accounts.clear(account_id)
        if kind != "youtube":
            raise Yt2BiliError("账号类型无效。")
        with self.mutation:
            self.ensure_idle()
            (self.paths.root / "secrets/youtube_cookies.txt").unlink(missing_ok=True)
        return {"cleared": True}

    def import_data(self, path):
        with self.mutation:
            self.ensure_idle()
            source = Path(path).resolve()
            db = source / "data/tasks.sqlite"
            if not db.is_file() or db.resolve() == self.store.path.resolve():
                raise Yt2BiliError("请选择另一个有效项目的数据目录。")
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as old:
                version = old.execute("PRAGMA user_version").fetchone()[0]
                if version > 5:
                    raise Yt2BiliError("不支持的来源数据库版本。")
                old.row_factory = sqlite3.Row
                rows = [dict(r) for r in old.execute("SELECT * FROM tasks")]
                tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "task_publications" in tables and old.execute("SELECT 1 FROM task_publications WHERE platform IN ('douyin','acfun')").fetchone():
                    raise Yt2BiliError("包含抖音或 AcFun 投稿的数据须整体备份/恢复，不能以旧版导入方式重建投稿身份；源数据未修改。")
                records = {r[0]: json.loads(r[1]) for r in old.execute("SELECT * FROM task_translation")} if "task_translation" in tables else {}
                attempts = [dict(r) for r in old.execute("SELECT * FROM translation_attempts ORDER BY id")] if "translation_attempts" in tables else []
            source_id = hashlib.sha256(str(db).encode()).hexdigest()
            imported, conflicts = 0, 0
            with self.store.transaction() as conn:
                for row in rows:
                    if not VIDEO_ID.fullmatch(row["video_id"]):
                        continue
                    legacy_id = row.get("task_id") or row["video_id"]
                    if conn.execute("SELECT 1 FROM legacy_task_map WHERE source_id=? AND legacy_id=?", (source_id, legacy_id)).fetchone():
                        continue
                    task = Task(**{k: v for k, v in row.items() if k in Task.__dataclass_fields__})
                    task.task_id = str(uuid.uuid4())
                    target = next((a for a in self.store.accounts(True) if version >= 2 and a["uid"] == row.get("account_uid_snapshot")), None)
                    task.account_id = target["account_id"] if target else None
                    task.account_uid_snapshot = target["uid"] if target else None
                    task.account_name_snapshot = (target["nickname"] or target["uid"]) if target else ""
                    existing = self.store.for_account(task.video_id, task.account_id) if target else None
                    if existing:
                        conn.execute("INSERT OR IGNORE INTO import_conflicts VALUES(?,?,?,?)",
                                     (source_id, legacy_id, json.dumps(row, ensure_ascii=False), existing.task_id))
                        conflicts += 1
                        continue
                    task.cancel_requested = 0
                    if task.work_dir:
                        candidate = source / "work" / (legacy_id if row.get("task_id") else task.video_id)
                        expected = candidate.resolve()
                        if Path(task.work_dir).resolve() != expected or candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()) or not expected.is_dir():
                            task.work_dir = task.work_root = task.video_path = task.cover_path = ""
                        else:
                            destination = Path(self.config.values["work_dir"]) / task.task_id
                            with work_lock(expected):
                                unsafe = any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in expected.rglob("*"))
                                if unsafe:
                                    raise Yt2BiliError("来源素材含目录跳转，请先整理素材后导入。")
                                shutil.copytree(expected, destination)
                            task.work_root, task.work_dir = str(destination.parent), str(destination)
                            for attr in ("video_path", "cover_path"):
                                if getattr(task, attr) and expected not in Path(getattr(task, attr)).resolve().parents:
                                    setattr(task, attr, "")
                                elif getattr(task, attr):
                                    setattr(task, attr, str(destination / Path(getattr(task, attr)).resolve().relative_to(expected)))
                    if task.status == "ready" and not task.work_dir:
                        task.status = "interrupted"
                    if task.status == "uploading" or task.status == "submitted" and not task.bv_id:
                        task.status = "submission_unknown"
                    elif task.status not in ("submitted", "ready", "failed", "cancelled", "submission_unknown"):
                        task.status = "interrupted"
                    self.store.upsert(task)
                    publications.ensure_bili(self.store, task)
                    from yt2bili.translation.config import legacy_snapshot
                    record = records.get(legacy_id) or {"config_snapshot": legacy_snapshot({})}
                    if task.title_zh and not record.get("state"):
                        record.update(state="legacy_preserved", provider="unknown", user_edited=False)
                    self.store.save_translation(task, record)
                    for attempt in attempts:
                        if (attempt.get("task_id") or attempt.get("video_id")) == legacy_id:
                            conn.execute("INSERT INTO translation_attempts(task_id,payload) VALUES(?,?)", (task.task_id, attempt["payload"]))
                    conn.execute("INSERT INTO legacy_task_map VALUES(?,?,?)", (source_id, legacy_id, task.task_id))
                    imported += 1
            return {"imported": imported, "conflicts": conflicts, "note": "素材复制到独立任务目录，源文件保留；未知归属请人工确认，未导入凭据。"}

    def log_tail(self, task_id=None, account_id=None):
        with self.logs_lock:
            return {"items": [entry for entry in self.logs
                             if (not task_id or entry.get("task_id") == task_id)
                             and (not account_id or entry.get("account_id") == account_id)][-300:]}

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
        if self._closed:
            return
        self.login_cancel()
        self.translation_jobs.close()
        self.scheduler.close()
        self.store.close()
        self.owner_lock.__exit__(None, None, None)
        self._closed = True

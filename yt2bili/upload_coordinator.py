"""Account-scoped submission boundary. Waiting never occupies another lane."""
from __future__ import annotations

import json
import logging
import os
import re
import math
import time
import uuid
from dataclasses import replace
from pathlib import Path

from yt2bili import bili_upload, desktop_auth, events, pipeline
from yt2bili.db import _now
from yt2bili.desktop_settings import atomic_json
from yt2bili.exceptions import Yt2BiliError
from yt2bili.locking import AccountBusy, account_guard, coordination_dir
from yt2bili.task_paths import validate_task_paths


class UploadCoordinator:
    def __init__(self, store, accounts):
        self.store, self.accounts = store, accounts

    def try_submit(self, item):
        task = self.store.require(item.task_id)
        account = self.store.account(task.account_id)
        if account["uid"] != task.account_uid_snapshot:
            raise Yt2BiliError("目标账号 UID 与任务不一致。")
        if account["auth_state"] in ("missing", "expired"):
            return "auth_required", None
        try:
            with account_guard(account["uid"]):
                result = self._locked(item, task, account)
        except AccountBusy:
            return "account_busy", time.monotonic() + 1
        if result[0] == "finished" and self.store.require(task.task_id).bv_id:
            try:
                self.cleanup(task.task_id)
            except OSError:
                self.store.update(task.task_id, cleanup_state="failed")
        return result

    def _locked(self, item, task, account):
        marker = coordination_dir(account["uid"]) / "attempt-state.json"
        gap = item.settings.upload_gap_seconds
        try:
            state = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
            if not isinstance(state, dict):
                raise ValueError("invalid marker object")
            if state.get("blocked"):
                return "rate_limited", None
            if state.get("inflight"):
                # No blind reclaim after a crash: the previous child may still exist.
                return "account_busy", time.monotonic() + 5
            ended = float(state.get("ended", 0))
            if not math.isfinite(ended):
                raise ValueError("invalid completion time")
        except (OSError, ValueError, TypeError):
            ended = time.time()
            atomic_json(marker, {"ended": ended, "recovered_corrupt_marker": True})
        remaining = min(gap, max(0, ended + gap - time.time()))
        if remaining:
            return "cooldown", time.monotonic() + remaining
        try:
            account, info = self.accounts.read(task.account_id)
            identity = desktop_auth.verify_credentials(info)
        except Yt2BiliError as exc:
            expired = "失效" in str(exc) or "不存在" in str(exc)
            self.store.update_account(task.account_id, auth_state="expired" if expired else "unavailable")
            return ("auth_required", None) if expired else ("auth_unverified", time.monotonic() + 30)
        if identity["uid"] != task.account_uid_snapshot:
            raise Yt2BiliError("在线验证 UID 与任务不一致，已阻止投稿。")
        self.store.update_account(task.account_id, auth_state="valid", verified_at=_now())
        events.check_cancelled()
        attempt_id = str(uuid.uuid4())
        private = self.accounts.root / "secrets/bilibili" / task.account_id / "attempts" / (attempt_id + ".json")
        atomic_json(private, info)
        settings = replace(item.settings, bili_cookies=private, account_id=task.account_id, account_uid=account["uid"])
        started = False
        armed = False
        bv = ""
        try:
            # Preflight and renewal do not consume an upload attempt.
            bili_upload.renew(settings)
            renewed = json.loads(private.read_text(encoding="utf-8"))
            if desktop_auth.credential_identity(renewed) != account["uid"]:
                raise Yt2BiliError("续期后的 UID 与目标账号不一致。")
            renewed_identity = desktop_auth.verify_credentials(renewed)
            self.accounts.bind(renewed, task.account_id, verify=False, locked=True, verified_identity=renewed_identity)
            with self.store.transaction() as conn:
                current = self.store.require(task.task_id)
                saved = self.store.get_job(task.task_id)
                if item.cancel.is_set() or current.cancel_requested or saved.get("run_id") != item.run_id:
                    raise events.Cancelled("任务已取消，未发起投稿。")
                if current.status not in ("queued_upload",):
                    raise Yt2BiliError("任务状态已变化，未发起投稿。")
                conn.execute("INSERT INTO upload_attempts(attempt_id,task_id,account_id,uid,run_id,prepared_at,outcome) VALUES(?,?,?,?,?,?,'prepared')",
                             (attempt_id, task.task_id, task.account_id, account["uid"], item.run_id, _now()))
                self.store.update(task.task_id, status="uploading", wait_reason="", error="")
            atomic_json(marker, {"inflight": attempt_id, "owner_pid": os.getpid(), "task_id": task.task_id})
            armed = True

            def on_started(pid):
                nonlocal started
                started = True
                atomic_json(marker, {"inflight": attempt_id, "owner_pid": os.getpid(), "child_pid": pid, "task_id": task.task_id})
                with self.store.transaction() as conn:
                    conn.execute("UPDATE upload_attempts SET command_started_at=? WHERE attempt_id=?", (_now(), attempt_id))

            bv = bili_upload.upload(settings, Path(task.video_path), Path(task.cover_path), task.title_zh,
                                    task.desc_zh, task.url, on_started=on_started)
            # Record actual completion before potentially slow DB/filesystem cleanup.
            atomic_json(marker, {"ended": time.time()})
            armed = False
            with self.store.transaction() as conn:
                conn.execute("UPDATE upload_attempts SET outcome=?,bv_id=?,ended_at=? WHERE attempt_id=?",
                             ("submitted" if bv else "unknown", bv, _now(), attempt_id))
                self.store.update(task.task_id, status="submitted" if bv else "submission_unknown", bv_id=bv,
                                  error="" if bv else "未取得 BV 号，请核对该账号创作中心。", cleanup_state="pending" if bv else "none")
        except BaseException as exc:
            if armed:
                try:
                    limited = bool(re.search(r"频繁|风控|限流|too many requests|rate.limit|\b429\b", str(exc), re.I))
                    atomic_json(marker, {"ended": time.time(), "blocked": limited} if started else {"ended": ended})
                except OSError:
                    pass  # Keep inflight marker to block this UID until explicitly checked.
            with self.store.transaction() as conn:
                conn.execute("UPDATE upload_attempts SET outcome=?,ended_at=?,error=? WHERE attempt_id=?",
                             ("unknown" if started else "not_started", _now(), type(exc).__name__, attempt_id))
                if started:
                    self.store.update(task.task_id, status="submitted" if bv else "submission_unknown", bv_id=bv,
                                      error="已获得 BV，后续保存/清理异常。" if bv else "投稿结果待核对，请检查该账号创作中心。")
                elif self.store.require(task.task_id).status == "uploading":
                    self.store.update(task.task_id, status="queued_upload")
            raise
        finally:
            try:
                private.unlink(missing_ok=True)
            except OSError:
                pass
        return "finished", None

    def cleanup(self, task_id):
        task = self.store.require(task_id)
        if not task.work_dir:
            return
        try:
            validate_task_paths(task)
        except Yt2BiliError:
            self.store.update(task_id, cleanup_state="failed")
            return
        if any(t.task_id != task_id and t.work_dir and Path(t.work_dir).resolve() == Path(task.work_dir).resolve() for t in self.store.list_all()):
            self.store.update(task_id, cleanup_state="failed")
            return
        folder = Path(task.work_dir)
        root = Path(task.work_root)
        if folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction()):
            self.store.update(task_id, cleanup_state="failed")
            return
        pipeline._detach_file_handler(folder / "pipeline.log", task_id)
        pipeline._remove_work_dir(root, folder)
        if not folder.exists():
            self.store.update(task_id, work_dir="", video_path="", cover_path="", cleanup_state="done")
        else:
            self.store.update(task_id, cleanup_state="failed")

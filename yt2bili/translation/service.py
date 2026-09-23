from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict

from yt2bili import events
from yt2bili.exceptions import Yt2BiliError
from yt2bili.locking import FileLock
from yt2bili.process_manager import creation_options
from yt2bili.translate import _is_zh_lang, clamp_title
from .config import snapshot, component_root
from .types import TranslationError, TranslationResult
from .process_tree import ProcessTree

_lock = threading.Lock()


@contextmanager
def execution_slot(root):
    while not _lock.acquire(timeout=.1):
        events.progress("translation_wait")
        events.check_cancelled()
    acquired = None
    try:
        lock = FileLock(root / "translation.lock")
        while True:
            events.check_cancelled()
            try:
                acquired = lock.__enter__()
                break
            except Yt2BiliError:
                events.progress("translation_wait")
                time.sleep(.1)
        yield
    finally:
        if acquired:
            acquired.__exit__(None, None, None)
        _lock.release()


def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **creation_options())
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait()


def isolated_request(payload, deadline):
    command = ([sys.executable, "--translation-request"] if getattr(sys, "frozen", False)
               else [sys.executable, "-m", "yt2bili.translation.worker"])
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               encoding="utf-8", cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]),
                               start_new_session=os.name != "nt", **creation_options())
    tree = ProcessTree(process)
    encoded = json.dumps(payload, ensure_ascii=False)
    try:
        while True:
            events.check_cancelled()
            if time.monotonic() >= deadline:
                raise TranslationError("TIMEOUT", "翻译超时，已停止本次请求。")
            try:
                output, _ = process.communicate(input=encoded, timeout=.1)
                break
            except subprocess.TimeoutExpired:
                encoded = None
        events.check_cancelled()
        if process.returncode != 0 or len(output) > 262144:
            raise TranslationError("SERVICE_UNAVAILABLE", "翻译请求进程异常退出。")
        try:
            reply = json.loads(output)
        except ValueError:
            raise TranslationError("OUTPUT_INVALID", "翻译进程返回无效结果。") from None
        if "error" in reply:
            error = reply["error"]
            raise TranslationError(error["code"], error["message"], retryable=error.get("retryable", False))
        return reply["result"]
    finally:
        tree.close()
        stop_process(process)
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()


def translate(settings, title, description, source_lang, title_limit, desc_body_limit, *, config=None, request_fn=None):
    config = snapshot(settings) if config is None else config
    events.check_cancelled()
    title, description = title.strip(), description.strip()
    if _is_zh_lang(source_lang) or not (title or description):
        return TranslationResult(clamp_title(title, title_limit), description, "none",
                                 skipped_reason="source_chinese" if _is_zh_lang(source_lang) else "empty")
    original_length = len(description)
    description = description[:max(desc_body_limit * 2, 400)]
    root = component_root(settings)
    request_fn = request_fn or isolated_request
    primary = config["translation_primary"]
    providers = [primary]
    if config["translation_fallback_enabled"]:
        providers.append("deepl" if primary == "local_llm" else "local_llm")
    attempts, errors = [], []
    with execution_slot(root):
        started = time.monotonic()
        total_deadline = started + config["translation_total_timeout_seconds"]
        for index, provider in enumerate(providers):
            events.check_cancelled()
            events.progress("translation_fallback" if index else "translating", force=True,
                            provider=provider, fallback_reason=errors[0].code if errors else None)
            payload = {"provider": provider, "config": config, "root": str(root),
                       "source": {"title": title, "description": description, "source_lang": source_lang}}
            if config.get("model_digest"):
                payload["expected_digest"] = config["model_digest"]
            deadline = min(total_deadline, time.monotonic() + (config["local_llm_timeout_seconds"] if provider == "local_llm" else 90))
            for attempt in range(2):
                tick = time.monotonic()
                try:
                    if tick >= deadline:
                        raise TranslationError("TIMEOUT", "翻译预算已耗尽。")
                    if provider == "deepl":
                        try:
                            getter = getattr(settings, "deepl_key_provider", None)
                            payload["key"] = getter() if getter else getattr(settings, "deepl_auth_key", "")
                        except Yt2BiliError:
                            raise TranslationError("CREDENTIAL_MISSING", "无法读取 DeepL 系统凭据；本地服务不受影响。") from None
                        if not payload["key"]:
                            raise TranslationError("CREDENTIAL_MISSING", "未配置 DeepL 密钥。")
                    value = request_fn(payload, deadline)
                    for key, source in (("title", title), ("description", description)):
                        if not source:
                            value[key] = ""
                        if not isinstance(value.get(key), str) or (source and not value[key].strip()):
                            raise TranslationError("OUTPUT_INVALID", "服务返回无效译文。", retryable=True)
                    events.check_cancelled()
                    attempts.append({"provider": provider, "attempt": attempt + 1, "elapsed_ms": int((time.monotonic()-tick)*1000), "code": "OK"})
                    result = TranslationResult(**value)
                    result.title = clamp_title(result.title, title_limit)
                    result.description = result.description.strip()
                    result.elapsed_ms = int((time.monotonic()-started)*1000)
                    result.input_truncated |= len(description) < original_length
                    result.fallback_used = bool(index)
                    result.fallback_reason = errors[0].code if errors else None
                    result.attempts = attempts
                    return result
                except TranslationError as exc:
                    attempts.append({"provider": provider, "attempt": attempt + 1, "elapsed_ms": int((time.monotonic()-tick)*1000), "code": exc.code})
                    if exc.code == "INPUT_INVALID":
                        raise
                    if exc.retryable and attempt == 0 and deadline - time.monotonic() > 1:
                        until = time.monotonic() + .5
                        while time.monotonic() < until:
                            events.check_cancelled()
                            time.sleep(.05)
                        continue
                    errors.append(exc)
                    logging.getLogger(__name__).warning("翻译服务 %s 失败：%s", provider, exc.code)
                    break
        error = TranslationError("ALL_PROVIDERS_FAILED", "；".join(str(e) for e in errors) + " 素材与已有译文已保留。")
        error.attempts = attempts
        raise error


def source_hash(title, description, language):
    return hashlib.sha256(json.dumps([title, description, language], ensure_ascii=False).encode()).hexdigest()

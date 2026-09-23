from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from yt2bili import events
from yt2bili.desktop_settings import atomic_json
from .http import open_local
from .runtime import manifest, runtime_path, sha256, local_session, model_status
from .service import execution_slot
from .types import TranslationError


def safe_extract(archive, destination, *, limit=12 * 1024**3):
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as bundle:
        if sum(i.file_size for i in bundle.infolist()) > limit:
            raise TranslationError("INVALID_ARCHIVE", "组件解压大小超出限制。")
        for info in bundle.infolist():
            target = (destination / info.filename).resolve()
            if (not target.is_relative_to(destination) or "\\" in info.filename or ":" in info.filename
                    or (info.external_attr >> 16) & 0o170000 == 0o120000):
                raise TranslationError("INVALID_ARCHIVE", "组件压缩包包含不安全路径。")
        for info in bundle.infolist():
            events.check_cancelled()
            target = destination / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, open(target, "wb") as out:
                while chunk := source.read(1024 * 1024):
                    events.check_cancelled()
                    out.write(chunk)


def download_runtime(root):
    spec = manifest()["runtime"]
    target = root / "downloads/runtime.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and sha256(target) == spec["sha256"]:
        return target
    partial = target.with_suffix(".part")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset >= spec["size"]:
        partial.unlink()
        offset = 0
    headers = {"User-Agent": "yt2bili-translation/1"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    with urlopen(Request(spec["url"], headers=headers), timeout=15) as response:
        resume = response.status == 206 and response.headers.get("Content-Range", "").startswith(f"bytes {offset}-")
        if response.status == 206 and not resume:
            raise TranslationError("DOWNLOAD_FAILED", "运行时下载续传范围不匹配。")
        if not resume:
            offset = 0
        with open(partial, "ab" if resume else "wb") as out:
            while chunk := response.read(1024 * 1024):
                events.check_cancelled()
                offset += len(chunk)
                if offset > spec["size"]:
                    raise TranslationError("DOWNLOAD_FAILED", "运行时下载大小不匹配。")
                out.write(chunk)
                events.progress("runtime_download", bytes=offset, total=spec["size"], percent=offset / spec["size"] * 100)
    if partial.stat().st_size != spec["size"] or sha256(partial) != spec["sha256"]:
        partial.unlink(missing_ok=True)
        raise TranslationError("CHECKSUM_FAILED", "运行时校验失败，请重新下载。")
    partial.replace(target)
    return target


def install_runtime(root, archive):
    if sha256(archive) != manifest()["runtime"]["sha256"]:
        raise TranslationError("CHECKSUM_FAILED", "运行时文件校验失败。")
    binary = runtime_path(root)
    staging = binary.parent.with_name(binary.parent.name + ".installing")
    if staging.exists():
        shutil.rmtree(staging)
    safe_extract(archive, staging)
    executable = staging / "ollama.exe"
    if not executable.is_file():
        raise TranslationError("INVALID_ARCHIVE", "压缩包缺少 ollama.exe。")
    atomic_json(staging / "installed.json", {"binary_sha256": sha256(executable)})
    # Execution slot excludes running managed requests during this replacement.
    previous = binary.parent.with_name(binary.parent.name + ".previous")
    if previous.exists():
        shutil.rmtree(previous)
    if binary.parent.exists():
        binary.parent.replace(previous)
    try:
        staging.replace(binary.parent)
    except Exception:
        if previous.exists():
            previous.replace(binary.parent)
        raise
    if previous.exists():
        shutil.rmtree(previous)


def verify_models(root):
    model_file = root / "models/manifests/registry.ollama.ai/library/qwen3/8b"
    if sha256(model_file) != manifest()["model"]["digest"]:
        raise TranslationError("MODEL_DIGEST_MISMATCH", "下载模型已与固定清单不同，未启用该模型。")
    description = json.loads(model_file.read_text(encoding="utf-8"))
    for layer in [description["config"], *description["layers"]]:
        digest = layer["digest"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise TranslationError("CHECKSUM_FAILED", "模型层摘要无效。")
        file = root / "models/blobs" / digest.replace(":", "-")
        events.progress("model_verify", force=True, message="正在校验模型文件")
        if not file.is_file() or file.stat().st_size != layer["size"] or sha256(file) != digest[7:]:
            raise TranslationError("CHECKSUM_FAILED", "模型文件不完整，请重试安装。")


def install(config, root, *, offline_path=None):
    root = Path(root)
    if config["local_llm_mode"] != "managed":
        raise TranslationError("INPUT_INVALID", "外部模式不修改用户已有模型；请切回应用管理模式安装。")
    if sys.platform != "win32" or platform.machine().upper() not in ("AMD64", "X86_64"):
        raise TranslationError("UNSUPPORTED_PLATFORM", "自动安装仅支持 Windows x64。")
    root.mkdir(parents=True, exist_ok=True)
    with execution_slot(root):
        required = manifest()["runtime"]["size"] * 4 + manifest()["model"]["size"] + 1024**3
        if shutil.disk_usage(root).free < required:
            raise TranslationError("DISK_FULL", f"翻译组件需要预留约 {required / 1024**3:.1f} GiB 可用空间。")
        if offline_path:
            staging = root / "offline-import"
            if staging.exists():
                shutil.rmtree(staging)
            try:
                safe_extract(offline_path, staging, limit=24 * 1024**3)
                verify_models(staging)
                install_runtime(root, staging / "runtime.zip")
                shutil.copytree(staging / "models", root / "models", dirs_exist_ok=True)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        else:
            install_runtime(root, download_runtime(root))
            with local_session(config, root, installing=True) as address:
                with open_local(address, "/api/pull", {"model": config["local_llm_model"], "stream": True}, timeout=30) as response:
                    complete = False
                    while True:
                        events.check_cancelled()
                        raw = response.readline(65537)
                        if not raw:
                            break
                        if len(raw) > 65536:
                            raise TranslationError("DOWNLOAD_FAILED", "模型下载响应过大。")
                        value = json.loads(raw)
                        if value.get("error"):
                            raise TranslationError("DOWNLOAD_FAILED", "模型下载失败，可重试以复用已有文件。")
                        complete |= value.get("status") == "success"
                        done, total = value.get("completed", 0), value.get("total", 0)
                        events.progress("model_download", bytes=done, total=total,
                                        percent=min(100, done / total * 100) if total else None)
                    if not complete:
                        raise TranslationError("DOWNLOAD_FAILED", "模型下载中断，可重试继续。")
                state = model_status(address, config["local_llm_model"])
                if state["state"] != "ready":
                    raise TranslationError(state["code"], state["message"])
            verify_models(root)
        installed = {"model_digest": manifest()["model"]["digest"], "runtime_version": manifest()["runtime"]["version"]}
        atomic_json(root / "deployment.json", installed)
        return {"installed": True, **installed, "message": "组件安装完成，请试译验证本机推理。"}


def export_bundle(root, destination):
    root, destination = Path(root), Path(destination)
    with execution_slot(root):
        verify_models(root)
        runtime = root / "downloads/runtime.zip"
        if not runtime.is_file() or sha256(runtime) != manifest()["runtime"]["sha256"]:
            raise TranslationError("LOCAL_UNAVAILABLE", "缺少已校验的运行时压缩包，请在线安装后再导出。")
        if destination.resolve().is_relative_to(root.resolve()):
            raise TranslationError("INPUT_INVALID", "离线包请保存到组件目录之外。")
        partial = destination.with_suffix(destination.suffix + ".part")
        try:
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED) as bundle:
                bundle.write(runtime, "runtime.zip")
                model_file = root / "models/manifests/registry.ollama.ai/library/qwen3/8b"
                bundle.write(model_file, model_file.relative_to(root).as_posix())
                value = json.loads(model_file.read_text())
                for layer in [value["config"], *value["layers"]]:
                    events.check_cancelled()
                    file = root / "models/blobs" / layer["digest"].replace(":", "-")
                    bundle.write(file, file.relative_to(root).as_posix())
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)
    return {"exported": True, "path": str(destination)}

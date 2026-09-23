from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from yt2bili import events
from yt2bili.process_manager import creation_options
from .http import request_json
from .types import TranslationError
from .process_tree import ProcessTree


def manifest():
    return json.loads(files("yt2bili.translation").joinpath("manifest.json").read_text(encoding="utf-8"))


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            events.check_cancelled()
            digest.update(chunk)
    return digest.hexdigest()


def runtime_path(root):
    return Path(root) / "runtime" / manifest()["runtime"]["version"] / "ollama.exe"


def model_status(address, model):
    version = request_json(address, "/api/version").get("version", "")
    models = request_json(address, "/api/tags", limit=1048576).get("models", [])
    for item in models:
        if item.get("name") == model or item.get("model") == model:
            if item.get("remote_model") or item.get("remote_host"):
                return {"state": "error", "code": "MODEL_NOT_LOCAL", "message": "此服务指向云模型，请安装本机模型。"}
            digest = str(item.get("digest", "")).removeprefix("sha256:")
            if digest != manifest()["model"]["digest"]:
                return {"state": "error", "code": "MODEL_DIGEST_MISMATCH", "message": "模型摘要与支持清单不一致，请安装固定版本。"}
            return {"state": "ready", "digest": digest, "runtime_version": version, "message": "模型已安装，可进行试译。"}
    return {"state": "missing", "code": "MODEL_MISSING", "message": "本地模型未安装，请先安装翻译组件。"}


def status(config, root):
    if config["local_llm_mode"] == "external":
        try:
            return model_status(config["local_llm_base_url"], config["local_llm_model"])
        except TranslationError as exc:
            return {"state": "unavailable", "code": exc.code, "message": str(exc)}
    root = Path(root)
    marker = root / "deployment.json"
    try:
        installed = json.loads(marker.read_text(encoding="utf-8"))
        if (not runtime_path(root).is_file() or installed["model_digest"] != manifest()["model"]["digest"]
                or installed["runtime_version"] != manifest()["runtime"]["version"]):
            raise ValueError()
        model_file = root / "models/manifests/registry.ollama.ai/library/qwen3/8b"
        if sha256(model_file) != installed["model_digest"]:
            raise ValueError()
        return {"state": "ready", "message": "本地组件已安装，将按需启动；可试译检测。", **installed}
    except (OSError, KeyError, ValueError):
        return {"state": "missing", "message": "尚未安装本地翻译组件。", "code": "MODEL_MISSING"}


@contextmanager
def local_session(config, root, *, installing=False):
    """Own only processes created here. A busy port never grants ownership."""
    address = config["local_llm_base_url"]
    if config["local_llm_mode"] == "external":
        yield address
        return
    if sys.platform != "win32" or platform.machine().upper() not in ("AMD64", "X86_64"):
        raise TranslationError("UNSUPPORTED_PLATFORM", "自动部署目前支持 Windows x64；其他平台可连接已安装的本机 Ollama。")
    root = Path(root)
    binary = runtime_path(root)
    if not binary.is_file():
        raise TranslationError("LOCAL_UNAVAILABLE", "本地运行时未安装。")
    try:
        marker = json.loads((binary.parent / "installed.json").read_text(encoding="utf-8"))
        if sha256(binary) != marker["binary_sha256"]:
            raise ValueError()
    except (OSError, KeyError, ValueError):
        raise TranslationError("LOCAL_UNAVAILABLE", "本地运行时校验失败，请重新安装。") from None
    try:
        request_json(address, "/api/version", timeout=.3)
    except TranslationError:
        pass
    else:
        raise TranslationError("PORT_IN_USE", "11435 端口已被占用；请关闭占用服务或使用外部模式。")
    env = {**os.environ, "OLLAMA_HOST": "127.0.0.1:11435", "OLLAMA_MODELS": str(root / "models"),
           "OLLAMA_NO_CLOUD": "1", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1"}
    # Do not log runtime output: model runners can include prompts in debug logs.
    env.pop("OLLAMA_DEBUG", None)
    process = subprocess.Popen([str(binary), "serve"], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **creation_options())
    tree = ProcessTree(process)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            events.check_cancelled()
            if process.poll() is not None:
                raise TranslationError("LOCAL_UNAVAILABLE", "本地运行时启动失败，可能是端口或驱动问题。")
            try:
                version = request_json(address, "/api/version", timeout=.5).get("version")
                if version != manifest()["runtime"]["version"]:
                    raise TranslationError("VERSION_MISMATCH", "本地运行时版本不匹配。")
                break
            except TranslationError as exc:
                if exc.code == "VERSION_MISMATCH":
                    raise
                time.sleep(.1)
        else:
            raise TranslationError("TIMEOUT", "启动本地运行时超时。")
        yield address
    finally:
        tree.close()
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **creation_options())
            if process.poll() is None:
                process.kill()
            process.wait()

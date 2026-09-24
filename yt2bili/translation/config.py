from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from yt2bili.exceptions import Yt2BiliError

DEFAULTS = {
    "translation_primary": "local_llm",
    "translation_fallback_enabled": True,
    "local_llm_mode": "managed",
    "local_llm_base_url": "http://127.0.0.1:11435",
    "local_llm_model": "qwen3:8b",
    "local_llm_num_ctx": 8192,
    # qwen3:8b can take about two minutes for a full metadata pair on a 4 GB
    # hybrid CPU/GPU system. Keep enough headroom for cold starts and normal
    # throughput variance while retaining a bounded, cancellable request.
    "local_llm_timeout_seconds": 300,
    "translation_total_timeout_seconds": 420,
}


def validate(values):
    result = {**DEFAULTS, **{k: v for k, v in values.items() if k in DEFAULTS}}
    if result["translation_primary"] not in ("local_llm", "deepl"):
        raise Yt2BiliError("请选择本地大模型或 DeepL。")
    if type(result["translation_fallback_enabled"]) is not bool:
        raise Yt2BiliError("自动切换必须是开关值。")
    if result["local_llm_mode"] not in ("managed", "external"):
        raise Yt2BiliError("本地运行模式无效。")
    if result["local_llm_model"] != "qwen3:8b":
        raise Yt2BiliError("当前支持的模型为 qwen3:8b。")
    for name, low, high in (("local_llm_num_ctx", 8192, 32768),
                            ("local_llm_timeout_seconds", 15, 600),
                            ("translation_total_timeout_seconds", 30, 1200)):
        if type(result[name]) is not int or not low <= result[name] <= high:
            raise Yt2BiliError(f"{name} 必须在 {low}～{high} 之间。")
    if result["translation_total_timeout_seconds"] <= result["local_llm_timeout_seconds"]:
        raise Yt2BiliError("翻译总限时必须大于本地翻译限时。")
    address = result["local_llm_base_url"]
    try:
        parsed = urlsplit(address)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1")
                or not parsed.port or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise Yt2BiliError("本地模型地址必须为带端口的回环 HTTP 地址，例如 http://127.0.0.1:11434。") from None
    result["local_llm_base_url"] = address.rstrip("/")
    if result["local_llm_mode"] == "managed" and address.rstrip("/") != DEFAULTS["local_llm_base_url"]:
        raise Yt2BiliError("应用管理模式使用固定地址 http://127.0.0.1:11435。")
    return result


def from_env():
    values = {}
    for key, default in DEFAULTS.items():
        raw = os.getenv(key.upper())
        if raw is None or not raw.strip():
            continue
        raw = raw.strip()
        if type(default) is bool:
            if raw.lower() not in ("true", "false", "1", "0"):
                raise Yt2BiliError(f"{key.upper()} 必须为 true 或 false。")
            values[key] = raw.lower() in ("true", "1")
        elif type(default) is int:
            try:
                values[key] = int(raw)
            except ValueError:
                raise Yt2BiliError(f"{key.upper()} 必须为整数。") from None
        else:
            values[key] = raw
    if values.get("local_llm_mode") == "external" and "local_llm_base_url" not in values:
        values["local_llm_base_url"] = "http://127.0.0.1:11434"
    return validate(values)


def snapshot(settings):
    return validate({key: getattr(settings, key, default) for key, default in DEFAULTS.items()})


def legacy_snapshot(value):
    result = dict(value)
    if "translation_primary" not in result:
        result.update(translation_primary="deepl", translation_fallback_enabled=False)
    return {**DEFAULTS, **result}


def component_root(settings=None):
    explicit = getattr(settings, "translation_root", None)
    if explicit:
        return Path(explicit)
    from yt2bili.paths import AppPaths
    return AppPaths.default().root / "translation"

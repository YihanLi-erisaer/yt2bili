"""Loopback HTTP only: no ambient proxies and no redirects."""
from __future__ import annotations

import json
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from .config import validate
from .types import TranslationError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def open_local(base_url, path, data=None, timeout=3):
    validate({"local_llm_mode": "external", "local_llm_base_url": base_url})
    request = Request(base_url + path, data=None if data is None else json.dumps(data).encode(),
                      headers={"Content-Type": "application/json"})
    try:
        return build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout)
    except HTTPError as exc:
        # Do not include response bodies: they can contain the source text.
        code = "MODEL_MISSING" if exc.code == 404 else "SERVICE_UNAVAILABLE"
        body = exc.read(65536).decode("utf-8", errors="replace").lower()
        exc.close()
        if exc.code == 500 and any(text in body for text in ("out of memory", "not enough memory", "unable to allocate")):
            raise TranslationError("LOCAL_OOM", "本地模型内存或显存不足，请释放资源后重试。") from None
        if exc.code == 401:
            code = "AUTH_FAILED"
        raise TranslationError(code, f"本地服务返回 HTTP {exc.code}。", retryable=exc.code in (429, 502, 503)) from None
    except (URLError, OSError, socket.timeout):
        raise TranslationError("LOCAL_UNAVAILABLE", "无法连接本地翻译服务，请检查运行时或地址。") from None


def request_json(base_url, path, data=None, timeout=3, limit=262144):
    with open_local(base_url, path, data, timeout) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise TranslationError("OUTPUT_INVALID", "本地服务响应过大。")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise TranslationError("OUTPUT_INVALID", "本地服务未返回有效 JSON。", retryable=True) from None
    if not isinstance(value, dict) or value.get("error"):
        raise TranslationError("SERVICE_UNAVAILABLE", "本地推理失败，请检查模型资源与运行时日志。")
    return value

"""Private pipe-only worker. Never pass credentials through argv or log payloads."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from .types import TranslationError


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    output = sys.stdout
    sys.stdout = sys.stderr
    try:
        payload = json.loads(sys.stdin.read(262144))
        if payload["provider"] == "local_llm":
            from .local_provider import translate
        elif payload["provider"] == "deepl":
            from .deepl_provider import translate
        else:
            raise TranslationError("INPUT_INVALID", "不支持的翻译服务。")
        result = {"result": translate(payload)}
    except TranslationError as exc:
        result = {"error": {"code": exc.code, "message": str(exc), "retryable": exc.retryable}}
    except Exception:
        result = {"error": {"code": "SERVICE_UNAVAILABLE", "message": "翻译请求未完成，请检测服务或模型。"}}
    output.write(json.dumps(result, ensure_ascii=False) + "\n")
    output.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

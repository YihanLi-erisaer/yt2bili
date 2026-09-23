from __future__ import annotations

import json
import re
from . import prompts
from .http import request_json
from .types import TranslationError


def translate(payload):
    from .runtime import local_session, model_status
    config, source = payload["config"], payload["source"]
    with local_session(config, payload["root"]) as address:
        status = model_status(address, config["local_llm_model"])
        if status["state"] != "ready":
            raise TranslationError(status["code"], status["message"])
        expected = payload.get("expected_digest")
        if expected and status["digest"] != expected:
            raise TranslationError("MODEL_DIGEST_MISMATCH", "模型版本与任务快照不一致，请恢复原模型或使用当前配置重试。")
        # Conservative UTF-8 byte upper bound for byte-BPE tokens; reserves template,
        # schema and output space without silently truncating the original title.
        # This deliberately trades some context utilization for an offline guarantee.
        body = source["description"]
        budget = config["local_llm_num_ctx"] - 3072 - 1024
        def messages(text):
            return [{"role": "system", "content": prompts.SYSTEM},
                    {"role": "user", "content": json.dumps({**source, "description": text}, ensure_ascii=False)}]
        while len(json.dumps(messages(body), ensure_ascii=False).encode("utf-8")) > budget:
            if not body:
                raise TranslationError("INPUT_INVALID", "标题超出模型上下文预算。")
            body = body[:max(0, len(body) - max(32, len(body) // 8))].rstrip()
        response = request_json(address, "/api/chat", {
            "model": config["local_llm_model"], "messages": messages(body),
            "stream": False, "think": False, "format": prompts.SCHEMA, "keep_alive": "60s",
            "options": {"temperature": .2, "num_ctx": config["local_llm_num_ctx"], "num_predict": 3072},
        }, timeout=config["local_llm_timeout_seconds"])
        message = response.get("message", {})
        if (response.get("done") is not True or response.get("done_reason") == "length"
                or message.get("tool_calls") or response.get("eval_count", 0) >= 3072):
            raise TranslationError("OUTPUT_INVALID", "模型输出未完整结束。", retryable=True)
        try:
            result = json.loads(message["content"])
        except (ValueError, KeyError, TypeError):
            raise TranslationError("OUTPUT_INVALID", "模型未返回规定的翻译格式。", retryable=True) from None
        if (not isinstance(result, dict) or set(result) != {"title", "description"}
                or any(not isinstance(v, str) for v in result.values())):
            raise TranslationError("OUTPUT_INVALID", "模型翻译字段无效。", retryable=True)
        for key in ("title", "description"):
            if (source[key].strip() and not result[key].strip()) or any(marker in result[key] for marker in ("<think>", "</think>", "<|im_start|>", "```")):
                raise TranslationError("OUTPUT_INVALID", "模型返回了空译文或非翻译内容。", retryable=True)
            supplied = source["title"] if key == "title" else body
            urls = [url.rstrip(".,;:!?)]}") for url in re.findall(r'https?://[^\s<>"，。]+', supplied)]
            if any(url not in result[key] for url in urls):
                raise TranslationError("OUTPUT_INVALID", "模型输出未保留原文链接。", retryable=True)
        return {**result, "provider": "local_llm", "model_digest": status["digest"],
                "runtime_version": status["runtime_version"], "prompt_version": prompts.VERSION,
                "input_truncated": body != source["description"]}

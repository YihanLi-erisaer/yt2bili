from __future__ import annotations

import deepl
from contextlib import closing
from .types import TranslationError


def translate(payload):
    key = payload.get("key", "")
    if not key:
        raise TranslationError("CREDENTIAL_MISSING", "未配置 DeepL 密钥。")
    # These SDK globals are isolated inside the request subprocess.
    deepl.http_client.max_network_retries = 0
    deepl.http_client.min_connection_timeout = 10
    from yt2bili.translate import _deepl_source
    source = payload["source"]
    try:
        with closing(deepl.Translator(key, send_platform_info=False)) as translator:
            if translator.get_usage().any_limit_reached:
                raise TranslationError("QUOTA_EXCEEDED", "DeepL 额度已用尽。")
            values = {}
            for name in ("title", "description"):
                text = source[name]
                if not text.strip():
                    values[name] = ""
                    continue
                args = dict(target_lang="ZH", preserve_formatting=True, split_sentences="0" if name == "title" else "1")
                lang = _deepl_source(source.get("source_lang"))
                if lang:
                    args["source_lang"] = lang
                result = translator.translate_text(text, **args)
                item = result[0] if isinstance(result, list) else result
                values[name] = text if str(getattr(item, "detected_source_lang", "")).upper().startswith("ZH") else item.text
                if not values[name].strip():
                    raise TranslationError("OUTPUT_INVALID", "DeepL 返回了空译文。")
            return {**values, "provider": "deepl"}
    except deepl.AuthorizationException:
        raise TranslationError("AUTH_FAILED", "DeepL 密钥无效或已失效。") from None
    except deepl.QuotaExceededException:
        raise TranslationError("QUOTA_EXCEEDED", "DeepL 额度已用尽。") from None
    except deepl.TooManyRequestsException:
        raise TranslationError("RATE_LIMITED", "DeepL 请求受限。", retryable=True) from None
    except deepl.DeepLException:
        raise TranslationError("NETWORK_ERROR", "DeepL 请求失败，请检查网络。", retryable=True) from None

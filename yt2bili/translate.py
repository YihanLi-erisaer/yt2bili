from __future__ import annotations

import logging
import re
import time

import deepl

from yt2bili.exceptions import Yt2BiliError

logger = logging.getLogger(__name__)

def translate_title_and_desc(
    auth_key: str,
    title: str,
    description: str,
    source_lang: str | None,
    title_limit: int,
    desc_body_limit: int,
) -> tuple[str, str]:
    title_zh = title.strip()
    desc_zh = description.strip()
    if _is_zh_lang(source_lang):
        logger.info("源语言已是中文，跳过翻译。")
        return clamp_title(title_zh, title_limit), desc_zh.strip()

    if not auth_key:
        raise Yt2BiliError(
            "未配置 DEEPL_AUTH_KEY。请复制 .env.example 为 .env 并填入 DeepL Free 密钥。"
        )

    translator = deepl.Translator(auth_key)
    _assert_quota(translator)

    if title_zh:
        title_zh = _translate(
            translator,
            title_zh,
            source_lang=source_lang,
            split_sentences="0",
        )
    if desc_zh:
        snippet = desc_zh[: max(desc_body_limit * 2, 400)]
        desc_zh = _translate(
            translator,
            snippet,
            source_lang=source_lang,
            split_sentences="1",
        )

    return clamp_title(title_zh, title_limit), desc_zh.strip()


def clamp_title(title: str, limit: int) -> str:
    title = title.replace("\n", " ").replace("\r", " ").strip()
    title = re.sub(r"\s+", " ", title)
    if len(title) <= limit:
        return title
    for sep in (" | ", " - ", " — ", "：", ": "):
        if sep in title:
            candidate = title.split(sep)[0].strip()
            if candidate and len(candidate) <= limit:
                return candidate
    stripped = re.sub(r"[\(（][^)）]*[\)）]", "", title).strip()
    stripped = re.sub(r"\s+", " ", stripped)
    if stripped and len(stripped) <= limit:
        return stripped
    return title[:limit]


def build_description(
    body: str,
    orig_title: str,
    uploader: str,
    source_url: str,
    limit: int,
) -> str:
    orig_title = orig_title.replace("\n", " ").strip()
    uploader = uploader.replace("\n", " ").strip()
    footer = (
        f"\n\n————————\n原标题：{orig_title}\n原作者：{uploader}\n原链接：{source_url}"
    )
    if len(footer) >= limit:
        return footer[-limit:]
    budget = limit - len(footer)
    text = (body or "").strip()
    if len(text) > budget:
        keep = max(0, budget - 1)
        text = text[:keep] + ("…" if keep else "")
    return text + footer


def _translate(
    translator: deepl.Translator,
    text: str,
    source_lang: str | None,
    split_sentences: str,
) -> str:
    if not text.strip():
        return ""
    lang = _deepl_source(source_lang)
    kwargs: dict = {
        "target_lang": "ZH",
        "preserve_formatting": True,
        "split_sentences": split_sentences,
    }
    if lang:
        kwargs["source_lang"] = lang

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            result = translator.translate_text(text, **kwargs)
            item = result[0] if isinstance(result, list) else result
            translated = item.text
            detected = (getattr(item, "detected_source_lang", None) or "").upper()
            if detected.startswith("ZH"):
                return text
            return translated
        except deepl.QuotaExceededException as exc:
            raise Yt2BiliError(
                "DeepL Free 本月额度已用完。可等待下月重置或升级 Pro。"
            ) from exc
        except deepl.TooManyRequestsException as exc:
            last_error = exc
            wait = 5 * attempt
            logger.warning("DeepL 限流，%s 秒后重试", wait)
            time.sleep(wait)
        except deepl.DeepLException as exc:
            last_error = exc
            logger.warning("DeepL 请求失败：%s", exc)
            time.sleep(2 ** attempt)
    raise Yt2BiliError(f"DeepL 翻译失败：{last_error}")


def _assert_quota(translator: deepl.Translator) -> None:
    try:
        usage = translator.get_usage()
    except deepl.DeepLException as exc:
        raise Yt2BiliError(f"无法查询 DeepL 额度（请检查密钥是否为 Free API Key）：{exc}") from exc
    if usage.any_limit_reached:
        raise Yt2BiliError("DeepL 额度已用尽。")


def _is_zh_lang(lang: str | None) -> bool:
    if not lang:
        return False
    return lang.split("-")[0].upper() == "ZH"


def _deepl_source(lang: str | None) -> str | None:
    if not lang:
        return None
    code = lang.split("-")[0].upper()
    mapping = {
        "EN": "EN",
        "JA": "JA",
        "KO": "KO",
        "FR": "FR",
        "DE": "DE",
        "ES": "ES",
        "IT": "IT",
        "PT": "PT",
        "RU": "RU",
        "ZH": None,
        "ZH-HANS": None,
        "ZH-HANT": None,
    }
    if code == "ZH":
        return None
    return mapping.get(code, None)

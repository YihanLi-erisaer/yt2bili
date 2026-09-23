from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from yt2bili import events, translate
from .config import snapshot
from .service import translate as translate_group, source_hash
from .types import TranslationError


def sync_files(task, folder):
    for name, content in (("title.txt", task.title_zh), ("desc.txt", task.desc_zh)):
        target = Path(folder) / name
        temporary = target.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)


def prepare(settings, store, task, meta, folder, *, force=False):
    record = store.translation(task.video_id) or {}
    if not force and (record.get("state") in ("complete", "edited", "legacy_preserved", "skipped") or task.title_zh):
        if not record.get("state"):
            record.update(state="legacy_preserved", provider="unknown", user_edited=False)
            store.save_translation(task, record)
        sync_files(task, folder)
        return
    config = snapshot(settings) if force else record.get("config_snapshot") or snapshot(settings)
    # Preserve the policy before I/O, including for failures and CLI tasks.
    if not force:
        record["config_snapshot"] = config
        store.save_translation(task, record)
    reserve = 80 + len(meta.title) + len(meta.uploader) + len(meta.webpage_url)
    try:
        result = translate_group(settings, meta.title, meta.description, meta.language,
                                 settings.title_limit, max(200, settings.desc_limit-reserve), config=config)
    except TranslationError as exc:
        store.translation_attempt(task.video_id, getattr(exc, "attempts", [{"code": exc.code}]))
        raise
    events.check_cancelled()
    new_record = {**asdict(result), "state": "skipped" if result.skipped_reason else "complete",
                  "config_snapshot": {**config, **({"model_digest": result.model_digest} if result.model_digest else {})},
                  "source_hash": source_hash(meta.title, meta.description, meta.language),
                  "postprocess_version": 1, "user_edited": False, "revision": record.get("revision", 0) + 1}
    title_before, desc_before = task.title_zh, task.desc_zh
    task.title_zh = result.title
    task.desc_zh = translate.build_description(result.description, meta.title, meta.uploader, meta.webpage_url, settings.desc_limit)
    try:
        store.save_translation(task, new_record)
    except Exception:
        task.title_zh, task.desc_zh = title_before, desc_before
        raise
    store.translation_attempt(task.video_id, result.attempts)
    sync_files(task, folder)

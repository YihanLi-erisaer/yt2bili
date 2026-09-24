from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    root: Path
    deepl_auth_key: str = field(repr=False)
    bili_cookies: Path
    biliup_bin: Path | None
    youtube_cookies: Path | None
    youtube_cookies_from_browser: str | None
    bili_tid: int
    bili_tags: str
    bili_line: str
    work_dir: Path
    data_dir: Path
    bin_dir: Path
    download_jobs: int = 1
    upload_gap_seconds: int = 20
    desc_limit: int = 2000
    title_limit: int = 80
    cover_width: int = 1280
    cover_height: int = 720
    account_id: str | None = None
    account_uid: str | None = None
    translation_primary: str = "local_llm"
    translation_fallback_enabled: bool = True
    local_llm_mode: str = "managed"
    local_llm_base_url: str = "http://127.0.0.1:11435"
    local_llm_model: str = "qwen3:8b"
    local_llm_num_ctx: int = 8192
    local_llm_timeout_seconds: int = 300
    translation_total_timeout_seconds: int = 420
    translation_root: Path | None = None
    deepl_key_provider: Callable[[], str] | None = field(default=None, repr=False, compare=False)


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    from yt2bili.translation.config import from_env

    bili_cookies = Path(os.getenv("BILI_COOKIES", str(ROOT / "secrets" / "bili_cookies.json")))
    if not bili_cookies.is_absolute():
        bili_cookies = ROOT / bili_cookies

    yt_cookies_raw = os.getenv("YOUTUBE_COOKIES", "").strip()
    youtube_cookies: Path | None = None
    if yt_cookies_raw:
        youtube_cookies = Path(yt_cookies_raw)
        if not youtube_cookies.is_absolute():
            youtube_cookies = ROOT / youtube_cookies
    else:
        default_yt_cookies = ROOT / "secrets" / "youtube_cookies.txt"
        if default_yt_cookies.is_file():
            youtube_cookies = default_yt_cookies

    browser_raw = os.getenv("YOUTUBE_COOKIES_FROM_BROWSER", "").strip()
    youtube_cookies_from_browser = browser_raw or None

    biliup_raw = os.getenv("BILIUP_BIN", "").strip()
    biliup_bin = Path(biliup_raw) if biliup_raw else None

    tags = os.getenv("BILI_TAGS", "转载").strip() or "转载"
    tid = int(os.getenv("BILI_TID", "171"))
    # Auto-probe often picks bldsa, whose CDN cert currently fails rustls on Windows.
    line = os.getenv("BILI_LINE", "tx").strip() or "tx"
    # Download/validation stay shared; upload concurrency belongs to account lanes.
    download_jobs = 1
    upload_gap_seconds = _env_int("UPLOAD_GAP_SECONDS", 20, lo=0, hi=600)

    work_dir = ROOT / "work"
    data_dir = ROOT / "data"
    bin_dir = ROOT / "bin"
    work_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    bili_cookies.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        root=ROOT,
        deepl_auth_key=os.getenv("DEEPL_AUTH_KEY", "").strip(),
        bili_cookies=bili_cookies,
        biliup_bin=biliup_bin,
        youtube_cookies=youtube_cookies,
        youtube_cookies_from_browser=youtube_cookies_from_browser,
        bili_tid=tid,
        bili_tags=tags,
        bili_line=line,
        work_dir=work_dir,
        data_dir=data_dir,
        bin_dir=bin_dir,
        download_jobs=download_jobs,
        upload_gap_seconds=upload_gap_seconds,
        **from_env(),
    )


def _env_int(name: str, default: int, *, lo: int, hi: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(lo, min(hi, value))

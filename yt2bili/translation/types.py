from __future__ import annotations

from dataclasses import dataclass, field
from yt2bili.exceptions import Yt2BiliError


class TranslationError(Yt2BiliError):
    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


@dataclass
class TranslationResult:
    title: str
    description: str
    provider: str
    model_digest: str | None = None
    prompt_version: str | None = None
    runtime_version: str | None = None
    elapsed_ms: int = 0
    fallback_used: bool = False
    fallback_reason: str | None = None
    skipped_reason: str | None = None
    input_truncated: bool = False
    attempts: list = field(default_factory=list)

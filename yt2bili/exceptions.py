class Yt2BiliError(Exception):
    """User-facing pipeline error."""


class InvalidMediaError(Yt2BiliError):
    """Media is truncated, corrupt, or missing a required stream."""

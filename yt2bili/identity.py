"""Shared single-video input and identity validation for GUI and CLI."""
import re
from urllib.parse import parse_qs, urlparse
from yt2bili.exceptions import Yt2BiliError

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_single_video_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise Yt2BiliError("请输入一个有效的 YouTube 视频链接。")
    value = value.strip()
    if not value or re.search(r"\s", value):
        raise Yt2BiliError("每次只能添加一个 YouTube 视频链接，请分别添加。")
    try:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise ValueError()
        host = (parsed.hostname or "").lower()
        video_id = ""
        if host in ("youtu.be", "www.youtu.be"):
            video_id = parsed.path.strip("/")
        elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
            if parsed.path == "/watch":
                values = parse_qs(parsed.query, keep_blank_values=True).get("v", [])
                video_id = values[0] if len(values) == 1 else ""
            else:
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 2 and parts[0] in ("shorts", "live", "embed"):
                    video_id = parts[1]
        if not VIDEO_ID.fullmatch(video_id) or "http://" in parsed.query or "https://" in parsed.query:
            raise ValueError()
    except ValueError as exc:
        raise Yt2BiliError("请输入一个有效的 YouTube 视频链接，不支持频道或播放列表。") from exc
    return video_id, "https://www.youtube.com/watch?v=" + video_id


def normalize_uid(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,20}", str(value)) or int(value) <= 0:
        raise Yt2BiliError("Bilibili UID 无效。")
    return str(int(value))

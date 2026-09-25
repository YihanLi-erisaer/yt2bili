from __future__ import annotations

import json
import os
from pathlib import Path

from yt2bili.config import Settings
from yt2bili.exceptions import Yt2BiliError
from yt2bili.translation.config import DEFAULTS, validate as validate_translation, legacy_snapshot


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    if os.name != "nt":
        path.chmod(0o600)


class DesktopSettings:
    def __init__(self, paths, vault=None):
        self.paths = paths
        self.path = paths.root / "settings.json"
        if vault is None:
            import keyring
            vault = keyring
        self.vault = vault
        self.vault_service = "StarDazz.yt2bili:" + str(paths.root)
        self.translation_upgrade_notice = False
        self.values = dict(work_dir=str(paths.root / "work"), bili_tid=171, bili_tags="转载",
                           bili_line="tx", upload_gap_seconds=20, theme="system",
                           hwaccel="auto", validation_cache=True, douyin_broker_url="", **DEFAULTS)
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.translation_upgrade_notice = "translation_primary" not in saved
                self.values.update(self.validate(saved))
            except (ValueError, TypeError, Yt2BiliError) as exc:
                raise Yt2BiliError("设置文件无效，请从备份恢复 settings.json。") from exc

    def key(self):
        try:
            return self.vault.get_password(self.vault_service, "deepl") or ""
        except Exception as exc:
            raise Yt2BiliError("无法读取系统凭据存储，请检查系统登录状态。") from exc

    def set_key(self, value):
        if not isinstance(value, str) or len(value) > 512:
            raise Yt2BiliError("密钥格式无效。")
        try:
            if value.strip():
                self.vault.set_password(self.vault_service, "deepl", value.strip())
            elif self.key():
                self.vault.delete_password(self.vault_service, "deepl")
        except Exception as exc:
            raise Yt2BiliError("系统凭据保存失败，未将密钥写入普通设置文件。") from exc

    def public(self):
        try:
            has_key, vault_error = bool(self.key()), ""
        except Yt2BiliError as exc:
            has_key, vault_error = False, str(exc)
        from yt2bili.translation.runtime import status
        local_ready = status(self.values, self.paths.root / "translation")["state"] == "ready"
        primary_ready = local_ready if self.values["translation_primary"] == "local_llm" else has_key
        ready = primary_ready or (self.values["translation_fallback_enabled"] and (local_ready or has_key))
        return {**self.values, "has_deepl_key": has_key, "vault_error": vault_error, "translation_ready": bool(ready),
                "translation_upgrade_notice": self.translation_upgrade_notice,
                "data_dir": str(self.paths.root), "youtube_cookies": (self.paths.root / "secrets/youtube_cookies.txt").is_file()}

    def validate(self, incoming):
        if not isinstance(incoming, dict) or set(incoming) - set(self.values):
            raise Yt2BiliError("包含不支持的设置项。")
        merged = {**self.values, **incoming}
        if not isinstance(merged["douyin_broker_url"], str):
            raise Yt2BiliError("抖音服务地址格式无效。")
        if merged["douyin_broker_url"]:
            from urllib.parse import urlsplit
            url = urlsplit(merged["douyin_broker_url"])
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
                raise Yt2BiliError("抖音服务地址必须为 HTTPS 源地址，不能携带凭据、路径或查询参数。")
        merged.update(validate_translation(merged))
        for name, lo, hi in (("bili_tid", 1, 65535), ("upload_gap_seconds", 0, 600)):
            if type(merged[name]) is not int or not lo <= merged[name] <= hi:
                raise Yt2BiliError(f"{name} 必须在 {lo}～{hi} 之间。")
        if merged["theme"] not in ("system", "dark", "light") or merged["hwaccel"] not in ("auto", "cpu"):
            raise Yt2BiliError("主题或校验模式无效。")
        if merged["bili_line"] not in ("tx", "bda2", "qn", "ws", "txa"):
            raise Yt2BiliError("请选择支持的上传线路。")
        if not isinstance(merged["bili_tags"], str) or not merged["bili_tags"].strip() or len(merged["bili_tags"]) > 200:
            raise Yt2BiliError("请填写有效标签（不超过 200 字）。")
        if type(merged["validation_cache"]) is not bool:
            raise Yt2BiliError("校验缓存必须为开关值。")
        if not isinstance(merged["work_dir"], str) or not Path(merged["work_dir"]).is_absolute():
            raise Yt2BiliError("工作目录必须为绝对路径。")
        target = Path(merged["work_dir"]).resolve()
        if target == target.parent:
            raise Yt2BiliError("请选择专用工作文件夹，不能使用磁盘根目录。")
        merged["work_dir"] = str(target)
        return merged

    def update(self, incoming):
        value = self.validate(incoming)
        folder = Path(value["work_dir"])
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".yt2bili-write-test"
        with open(probe, "xb") as handle:
            handle.write(b"ok")
        probe.unlink()
        atomic_json(self.path, value)
        self.values = value
        self.translation_upgrade_notice = False
        return self.public()

    def snapshot(self):
        return dict(self.values)

    def build(self, snapshot=None):
        value = legacy_snapshot(snapshot) if snapshot else self.values
        root = self.paths.root
        cookie = root / "secrets/youtube_cookies.txt"
        return Settings(root=root, deepl_auth_key="", deepl_key_provider=self.key,
                        bili_cookies=root / "secrets/bili_cookies.json", biliup_bin=None,
                        youtube_cookies=cookie if cookie.is_file() else None, youtube_cookies_from_browser=None,
                        bili_tid=value["bili_tid"], bili_tags=value["bili_tags"], bili_line=value["bili_line"],
                        upload_gap_seconds=value["upload_gap_seconds"], work_dir=Path(value["work_dir"]),
                        data_dir=root / "data", bin_dir=self.paths.resources / "bin",
                        translation_root=root / "translation", **validate_translation(value))


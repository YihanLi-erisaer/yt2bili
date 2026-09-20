"""Desktop paths are independent of the installation and working directory."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    root: Path
    resources: Path

    @classmethod
    def default(cls, root: str | None = None, resources: str | None = None):
        if root:
            base = Path(root).expanduser().resolve()
        elif sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "StarDazz/yt2bili"
        elif sys.platform == "darwin":
            base = Path.home() / "Library/Application Support/StarDazz/yt2bili"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "stardazz/yt2bili"
        resource_root = Path(resources) if resources else Path(__file__).resolve().parent.parent
        paths = cls(base, resource_root.resolve())
        for name in ("data", "work", "logs", "secrets"):
            (base / name).mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            (base / "secrets").chmod(0o700)
        return paths


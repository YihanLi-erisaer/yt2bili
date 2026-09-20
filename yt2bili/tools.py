from __future__ import annotations

import os
import shutil
from pathlib import Path


def find_tool(name: str, bin_dir: Path | None = None) -> str:
    folder = bin_dir or Path(os.environ.get("YT2BILI_BIN_DIR", Path(__file__).resolve().parent.parent / "bin"))
    names = (name + ".exe", name) if os.name == "nt" else (name,)
    for candidate in names:
        path = folder / candidate
        if path.is_file():
            return str(path.resolve())
    return shutil.which(name) or name


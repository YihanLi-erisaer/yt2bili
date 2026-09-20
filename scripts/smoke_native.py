"""Native WebView -> Rust -> Python startup check; requires the Vite dev server."""
from pathlib import Path
import json
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parent.parent
executable = root / "desktop/src-tauri/target/debug/yt2bili-desktop.exe"
with tempfile.TemporaryDirectory(prefix="yt2bili-native-") as folder:
    report = Path(folder) / "native.json"
    environment = {**os.environ, "YT2BILI_NATIVE_SMOKE_REPORT": str(report),
                   "YT2BILI_DESKTOP_DATA": folder, "YT2BILI_PROJECT_ROOT": str(root),
                   "YT2BILI_PYTHON": str(root / ".desktop-venv/Scripts/python.exe")}
    result = subprocess.run([str(executable)], env=environment, cwd=root, timeout=45, capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    value = json.loads(report.read_text(encoding="utf-8"))
    assert value.get("ok") and value.get("protocol_version") == 1, value
    print(json.dumps(value))

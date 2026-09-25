"""Native WebView -> Rust -> Python check; --release tests a packaged app offline."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--release", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
executable = args.release.resolve() if args.release else root / "desktop/src-tauri/target/debug/yt2bili-desktop.exe"
with tempfile.TemporaryDirectory(prefix="yt2bili-native-") as folder:
    report = Path(folder) / "native.json"
    environment = {**os.environ, "YT2BILI_NATIVE_SMOKE_REPORT": str(report),
                   "YT2BILI_DESKTOP_DATA": folder, "YT2BILI_PROJECT_ROOT": str(root),
                   "YT2BILI_PYTHON": os.environ.get("YT2BILI_PYTHON", str(root / ".desktop-venv/Scripts/python.exe"))}
    if args.release:
        for key in ("YT2BILI_PROJECT_ROOT", "YT2BILI_PYTHON", "YT2BILI_WORKER", "YT2BILI_RESOURCES", "PYTHONPATH"):
            environment.pop(key, None)
        environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    result = subprocess.run([str(executable), "--smoke-test"], env=environment, cwd=folder, timeout=60, capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    value = json.loads(report.read_text(encoding="utf-8"))
    assert value.get("ok") and value.get("protocol_version") == 2, value
    print(json.dumps(value))

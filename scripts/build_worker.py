"""Run with the desktop virtual environment; outputs an onedir worker."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
           "--name", "yt2bili-worker", "--paths", str(root),
           "--distpath", str(root / "packaging/staging"), "--workpath", str(root / "build/worker"),
           "--specpath", str(root / "build"), "--collect-all", "yt_dlp", "--collect-all", "yt_dlp_ejs",
           "--collect-all", "keyring", "--collect-all", "qrcode", str(root / "packaging/worker_entry.py")]
subprocess.run(command, cwd=root, check=True)
print("Worker built at", root / "packaging/staging/yt2bili-worker")

"""Archive the complete Windows application directory and record artifact hashes."""
import hashlib
import json
from pathlib import Path
import shutil

root = Path(__file__).resolve().parent.parent
output = root / "dist/windows"
version = json.loads((root / "desktop/package.json").read_text(encoding="utf-8"))["version"]
installer = output / f"yt2bili_{version}_x64-setup.exe"
if not installer.is_file():
    raise FileNotFoundError(f"Installer is missing: {installer}")
shutil.copy2(root / "docs/Windows安装与打包.md", output / "使用说明.md")
archive = Path(shutil.make_archive(str(output / f"yt2bili_{version}_x64-portable"), "zip", output, "yt2bili"))
files = [installer, archive]
lines = []
for file in files:
    with file.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    lines.append(f"{digest}  {file.name}")
(output / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))

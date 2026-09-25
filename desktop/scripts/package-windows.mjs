import { copyFileSync, cpSync, mkdirSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

if (process.platform !== "win32") throw new Error("Build this installer on Windows x64.");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const python = process.env.YT2BILI_BUILD_PYTHON || [
  ".desktop-dev/package-python/Scripts/python.exe",
  ".desktop-venv/Scripts/python.exe",
].map((entry) => path.join(root, entry)).find(existsSync);
if (!python) throw new Error("Set YT2BILI_BUILD_PYTHON to a Python environment with requirements-desktop-lock.txt installed.");
function run(executable, args) {
  const result = spawnSync(executable, args, { cwd: root, stdio: "inherit" });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
const tools = path.join(root, "packaging/staging/windows-bin");
mkdirSync(tools, { recursive: true });
for (const name of ["ffmpeg.exe", "ffprobe.exe", "biliup.exe"]) {
  const source = path.join(root, "bin", name);
  if (!existsSync(source)) throw new Error(`Missing bundled tool: ${source}`);
  copyFileSync(source, path.join(tools, name));
}
copyFileSync(process.execPath, path.join(tools, "node.exe"));
run(python, ["scripts/build_worker.py"]);
run(python, ["scripts/smoke_worker.py", "--frozen", "packaging/staging/yt2bili-worker/yt2bili-worker.exe"]);
run(process.execPath, ["desktop/scripts/tauri.mjs", "build", "--no-bundle", "--config", "src-tauri/tauri.windows-release.conf.json", "--", "--locked"]);
const release = path.join(root, "desktop/src-tauri/target/release");
const portable = path.join(root, "dist/windows/yt2bili");
mkdirSync(portable, { recursive: true });
copyFileSync(path.join(release, "yt2bili-desktop.exe"), path.join(portable, "yt2bili.exe"));
cpSync(path.join(root, "packaging/staging/yt2bili-worker"), path.join(portable, "worker"), { recursive: true });
cpSync(tools, path.join(portable, "bin"), { recursive: true });
run(python, ["scripts/smoke_worker.py", "--frozen", path.join(portable, "worker/yt2bili-worker.exe"), "--resources", portable]);
run(python, ["scripts/smoke_native.py", "--release", path.join(portable, "yt2bili.exe")]);
run(process.execPath, ["desktop/scripts/tauri.mjs", "bundle", "--config", "src-tauri/tauri.windows-release.conf.json"]);
cpSync(path.join(release, "bundle/nsis"), path.join(root, "dist/windows"), { recursive: true });
run(python, ["scripts/archive_windows.py"]);
console.log(`Windows installer and application: ${path.join(root, "dist/windows")}`);

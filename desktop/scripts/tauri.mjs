import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn, spawnSync } from "node:child_process";
import { launchEnvironment } from "./launch-env.mjs";

const root = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const toolchains = path.join(root, ".toolchains");
const cargo = path.join(toolchains, "cargo/bin");
const localRust = existsSync(cargo);
const env = launchEnvironment(process.env, {
  cargo: localRust ? cargo : undefined,
});
if (localRust) {
  env.CARGO_HOME = path.join(toolchains, "cargo");
  env.RUSTUP_HOME = path.join(toolchains, "rustup");
}
const python = path.join(
  root,
  ".desktop-venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);
if (!env.YT2BILI_PYTHON && existsSync(python)) env.YT2BILI_PYTHON = python;
env.YT2BILI_PROJECT_ROOT = root;
if (process.argv[2] === "dev" && !env.YT2BILI_WORKER) {
  const check = spawnSync(
    env.YT2BILI_PYTHON || python,
    [
      "-c",
      "import sqlite3, yt_dlp, deepl, keyring, qrcode; from PIL import _imaging",
    ],
    {
      cwd: root,
      env,
      encoding: "utf8",
      windowsHide: true,
      timeout: 30000,
    },
  );
  if (check.error || check.status !== 0) {
    console.error(
      "桌面 Python 环境无法启动。若更换过 Python 版本，请重新安装对应版本的依赖。\n在项目根目录运行：\n.desktop-venv/Scripts/python.exe -m pip install --force-reinstall -r requirements-desktop-lock.txt",
    );
    console.error(check.error?.message || check.stderr.trim());
    process.exit(1);
  }
}
const cli = path.join(root, "desktop/node_modules/@tauri-apps/cli/tauri.js");
const child = spawn(process.execPath, [cli, ...process.argv.slice(2)], {
  cwd: path.join(root, "desktop"),
  stdio: "inherit",
  env,
});
child.on("exit", (code) => process.exit(code ?? 1));
child.on("error", (error) => {
  console.error(`无法启动 Tauri：${error.message}`);
  process.exitCode = 1;
});

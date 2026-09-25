import { spawnSync } from "node:child_process";
for (const [entry, args] of [
  ["typescript/bin/tsc", ["--noEmit"]],
  ["vite/bin/vite.js", ["build"]],
]) {
  const result = spawnSync(process.execPath, [`node_modules/${entry}`, ...args], { stdio: "inherit" });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

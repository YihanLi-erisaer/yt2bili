import path from "node:path";

// A plain JS object can contain both PATH and Path. Windows child_process
// forwards only one of them, so normalize before adding development tools.
export function launchEnvironment(
  source,
  { platform = process.platform, node = process.execPath, cargo } = {},
) {
  const env = { ...source };
  const windows = platform === "win32";
  const paths = windows ? path.win32 : path.posix;
  const keys = Object.keys(env).filter((key) =>
    windows ? key.toLowerCase() === "path" : key === "PATH",
  );
  const entries = [
    cargo,
    paths.dirname(node),
    ...keys.flatMap((key) => (env[key] || "").split(paths.delimiter)),
  ];
  for (const key of keys) delete env[key];
  const seen = new Set();
  env[windows ? "Path" : "PATH"] = entries
    .filter((entry) => {
      if (!entry) return false;
      const key = windows ? entry.toLowerCase() : entry;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .join(paths.delimiter);
  return env;
}

import test from "node:test";
import assert from "node:assert/strict";
import { launchEnvironment } from "./launch-env.mjs";

test("Windows Path-only environments retain npm and system tools", () => {
  const source = {
    Path: "C:\\Program Files\\nodejs;C:\\Windows\\System32",
    SystemRoot: "C:\\Windows",
  };
  const env = launchEnvironment(source, {
    platform: "win32",
    node: "C:\\Program Files\\nodejs\\node.exe",
    cargo: "C:\\project\\cargo\\bin",
  });
  assert.equal(
    env.Path,
    "C:\\project\\cargo\\bin;C:\\Program Files\\nodejs;C:\\Windows\\System32",
  );
  assert.equal(env.PATH, undefined);
  assert.equal(env.SystemRoot, source.SystemRoot);
  assert.equal(source.Path, "C:\\Program Files\\nodejs;C:\\Windows\\System32");
});

test("Windows merges conflicting PATH casing before spawning children", () => {
  const env = launchEnvironment(
    {
      PATH: "C:\\rust;C:\\Windows",
      Path: "C:\\node;C:\\WINDOWS",
      path: "C:\\other",
    },
    { platform: "win32", node: "C:\\node\\node.exe" },
  );
  assert.deepEqual(Object.keys(env), ["Path"]);
  assert.equal(env.Path, "C:\\node;C:\\rust;C:\\Windows;C:\\other");
  assert.ok(!env.Path.includes("undefined"));
});

test("POSIX keeps case-sensitive environment names and colon separators", () => {
  const env = launchEnvironment(
    { PATH: "/usr/bin:/bin", Path: "unrelated" },
    { platform: "darwin", node: "/opt/node/bin/node", cargo: "/opt/cargo/bin" },
  );
  assert.equal(env.PATH, "/opt/cargo/bin:/opt/node/bin:/usr/bin:/bin");
  assert.equal(env.Path, "unrelated");
});

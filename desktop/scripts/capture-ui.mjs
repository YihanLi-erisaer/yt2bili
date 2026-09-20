// Requires `npm run dev`; screenshots use clearly labelled sample data.
import { chromium } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
const output = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../docs/screenshots");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: "msedge", headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });
  await page.goto("http://127.0.0.1:1420/?preview&populated");
  await page.getByText("用更少的工具，构建更专注的工作流", { exact: true }).waitFor();
  await page.screenshot({ path: path.join(output, "tasks-dark.png"), animations: "disabled" });
  await page.getByRole("button", { name: "账号与连接", exact: true }).click();
  await page.getByRole("heading", { name: "DeepL 翻译" }).waitFor();
  await page.screenshot({ path: path.join(output, "account-dark.png"), animations: "disabled" });
  await page.getByRole("button", { name: /^任务中心/ }).click();
  await page.getByRole("button", { name: "切换明暗主题" }).click();
  await page.locator('html[data-theme="light"]').waitFor();
  await page.screenshot({ path: path.join(output, "tasks-light.png"), animations: "disabled" });
} finally { await browser.close(); }

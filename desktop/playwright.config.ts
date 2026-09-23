import { defineConfig } from "@playwright/test";
const testPort = process.env.YT2BILI_TEST_PORT || "1420";
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  workers: 1,
  timeout: 60000,
  use: {
    baseURL: `http://127.0.0.1:${testPort}`,
    channel: "msedge",
    viewport: { width: 1280, height: 800 },
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `node node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${testPort}`,
    url: `http://127.0.0.1:${testPort}`,
    reuseExistingServer: true,
  },
});

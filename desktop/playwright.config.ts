import { defineConfig } from "@playwright/test";
const testPort = process.env.YT2BILI_TEST_PORT || "1420";
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  use: {
    baseURL: `http://127.0.0.1:${testPort}`,
    channel: "msedge",
    viewport: { width: 1280, height: 800 },
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `npm run dev -- --port ${testPort}`,
    url: `http://127.0.0.1:${testPort}`,
    reuseExistingServer: !process.env.YT2BILI_TEST_PORT,
  },
});

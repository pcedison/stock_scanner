const { defineConfig, devices } = require("@playwright/test");
const path = require("node:path");

const port = Number(process.env.E2E_PORT || 8010);
const baseURL = `http://127.0.0.1:${port}`;

module.exports = defineConfig({
  testDir: path.join(__dirname, "tests", "e2e"),
  testMatch: /.*\.spec\.ts/,
  timeout: 30_000,
  workers: 1,
  expect: {
    timeout: 5_000,
  },
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile-chrome", use: { ...devices["Pixel 7"] } },
  ],
  webServer: {
    command: `node tests/e2e/start-server.cjs ${port}`,
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});

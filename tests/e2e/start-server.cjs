const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..", "..");
const port = Number(process.argv[2] || process.env.E2E_PORT || 8010);
const tempDir = path.join(root, ".tmp", "e2e");
fs.mkdirSync(tempDir, { recursive: true });

const settingsPath = path.join(tempDir, "settings.json");
fs.writeFileSync(
  settingsPath,
  JSON.stringify(
    {
      auto_scan_full_market: true,
      manual_scan_enabled: true,
      exclude_financial_industry: true,
      use_mock_data: true,
      scan_twse: true,
      scan_tpex: true,
      spring_festival_guard: true,
      revenue_growth_mode: "cumulative_ytd",
    },
    null,
    2,
  ),
);

const env = {
  ...process.env,
  AUTH_DB_PATH: path.join(tempDir, "auth.sqlite3"),
  SETTINGS_PATH: settingsPath,
  SESSION_COOKIE_SECURE: "0",
};

const child = spawn(
  process.env.PYTHON || "python",
  ["-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", String(port)],
  {
    cwd: root,
    env,
    stdio: "inherit",
  },
);

const shutdown = () => {
  if (!child.killed) child.kill("SIGTERM");
};

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
child.on("exit", (code, signal) => {
  if (signal) process.exit(0);
  process.exit(code || 0);
});

import subprocess
from pathlib import Path


def test_playwright_scans_only_e2e_tests():
    completed = subprocess.run(
        [
            "node",
            "-e",
            """
            const Module = require("node:module");
            const originalLoad = Module._load;
            Module._load = function (request, parent, isMain) {
              if (request === "@playwright/test") {
                return {
                  defineConfig: (config) => config,
                  devices: {
                    "Desktop Chrome": { use: {} },
                    "Pixel 5": { use: {} },
                  },
                };
              }
              return originalLoad.apply(this, arguments);
            };
            const cfg = require("./playwright.config.js");
            console.log(cfg.testDir);
            """,
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )

    test_dir = Path(completed.stdout.strip()).as_posix()

    assert test_dir.endswith("/tests/e2e")

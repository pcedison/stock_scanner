from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_BUDGETS = {
    "frontend/app.js": 2010,
    "frontend/dom.js": 90,
    "frontend/renderers.js": 300,
    "frontend/strategy_content.js": 160,
    "frontend/storage.js": 80,
    "frontend/navigation.js": 100,
    "frontend/normalize.js": 220,
    "frontend/market_scan.js": 200,
    "frontend/market_render.js": 220,
    "frontend/reference_data.js": 40,
    "frontend/styles.css": 2953,
    "cloudflare/worker.py": 880,
    "cloudflare/worker_support.py": 590,
    ".github/workflows/cloudflare-r2-seed-refresh.yml": 260,
}


def line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def collect_budget_report(root: Path = ROOT_DIR, budgets: dict[str, int] | None = None) -> dict[str, object]:
    budgets = budgets or DEFAULT_BUDGETS
    files = []
    problems = []
    for relative, budget in budgets.items():
        path = root / relative
        count = line_count(path)
        item = {"path": relative, "lines": count, "budget": budget, "ok": count <= budget}
        files.append(item)
        if count > budget:
            problems.append(f"{relative} has {count} lines; budget is {budget}")
    return {"files": files, "problems": problems, "ok": not problems}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check line-count budgets for high-churn files.")
    parser.add_argument("--root", type=Path, default=ROOT_DIR)
    args = parser.parse_args(argv)

    report = collect_budget_report(args.root)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["problems"]:
        print("Code size budget check failed:", file=sys.stderr)
        for problem in cast(list[str], report["problems"]):
            print(f"- {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

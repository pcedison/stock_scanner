from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_BUDGETS = {
    "backend/services/market_query.py": 350,
    "frontend/app.js": 2050,
    "frontend/dom.js": 90,
    "frontend/renderers.js": 300,
    "frontend/strategy_content.js": 160,
    "frontend/storage.js": 80,
    "frontend/navigation.js": 100,
    "frontend/normalize.js": 220,
    "frontend/market_scan.js": 200,
    "frontend/market_render.js": 220,
    "frontend/market_query.js": 520,
    "frontend/market_refresh.js": 220,
    "frontend/reference_data.js": 40,
    "frontend/styles.css": 2953,
    # 900 -> 905: the trading-calendar rule lives in cloudflare/worker_trading_calendar.py,
    # so only an import and a one-line call site landed here. tests/test_code_size_budgets.py
    # pins this number on purpose - raising it is meant to be a deliberate, reviewed act.
    "cloudflare/worker.py": 905,
    "cloudflare/worker_trading_calendar.py": 90,
    "cloudflare/worker_health.py": 120,
    "cloudflare/worker_market_query.py": 450,
    "cloudflare/worker_refresh_jobs.py": 260,
    "cloudflare/worker_refresh_control.py": 180,
    "cloudflare/worker_refresh_schedule.py": 170,
    # Raised from 170 when js_fetch_options landed: this module owns the Python/JS
    # boundary for outbound GitHub calls, and getting that conversion wrong kills the
    # interpreter outright rather than raising.
    "cloudflare/worker_github_app.py": 200,
    "cloudflare/worker_market_resilience.py": 160,
    "cloudflare/worker_market_legacy.py": 120,
    "cloudflare/worker_observability.py": 200,
    "cloudflare/worker_support.py": 600,
    ".github/workflows/cloudflare-r2-seed-refresh.yml": 290,
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

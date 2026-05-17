from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any


DEFAULT_ZIP = Path("data/official_cache_seed_2026-05-14.zip")
REQUIRED_ENTRIES = {
    "official_fundamentals_history.json",
    "official_history_backfill_progress.json",
}
MIN_HISTORY_COMPANIES = 1000
MIN_HISTORY_ROWS = 5000


def _load_json_from_zip(archive: zipfile.ZipFile, name: str) -> Any:
    try:
        with archive.open(name) as handle:
            return json.loads(handle.read().decode("utf-8"))
    except KeyError as exc:
        raise ValueError(f"Missing required seed entry: {name}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Seed entry is not valid JSON: {name}") from exc


def validate_seed_zip(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"Seed zip does not exist: {path}")

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        missing = sorted(REQUIRED_ENTRIES - names)
        if missing:
            raise ValueError(f"Seed zip is missing required entries: {', '.join(missing)}")

        history = _load_json_from_zip(archive, "official_fundamentals_history.json")
        _load_json_from_zip(archive, "official_history_backfill_progress.json")

    if not isinstance(history, dict):
        raise ValueError("official_fundamentals_history.json must be a JSON object")
    quarters = history.get("quarters")
    if not isinstance(quarters, dict):
        raise ValueError("official_fundamentals_history.json must contain a quarters object")

    company_count = 0
    row_count = 0
    latest_period = None
    for stock_code, records in quarters.items():
        if not isinstance(stock_code, str) or not isinstance(records, dict) or not records:
            continue
        company_count += 1
        row_count += len(records)
        latest_for_company = max(records)
        latest_period = max(latest_period or latest_for_company, latest_for_company)

    if company_count < MIN_HISTORY_COMPANIES:
        raise ValueError(f"Seed history has only {company_count} companies; expected at least {MIN_HISTORY_COMPANIES}")
    if row_count < MIN_HISTORY_ROWS:
        raise ValueError(f"Seed history has only {row_count} quarterly rows; expected at least {MIN_HISTORY_ROWS}")

    return {
        "zip": str(path),
        "companies": company_count,
        "quarterlyRows": row_count,
        "latestPeriod": latest_period,
        "requiredEntries": sorted(REQUIRED_ENTRIES),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the committed Cloudflare seed cache inputs.")
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP, help="Path to the committed seed zip")
    args = parser.parse_args(argv)

    try:
        summary = validate_seed_zip(args.zip)
    except ValueError as exc:
        print(f"Seed validation failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

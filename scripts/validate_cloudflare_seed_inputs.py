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
    "cloudflare_seed/manifest.json",
    "cloudflare_seed/companies.json",
    "cloudflare_seed/data_sources_status.json",
    "cloudflare_seed/market_scan_latest.json",
    "cloudflare_seed/analysis_by_code.json",
}
MIN_HISTORY_COMPANIES = 1000
MIN_HISTORY_ROWS = 5000
MIN_SEED_COMPANIES = 1000
MIN_SEED_ANALYSIS = 1000


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
        manifest = _load_json_from_zip(archive, "cloudflare_seed/manifest.json")
        analysis_shards = sorted(
            name for name in names if name.startswith("cloudflare_seed/analysis_shards/") and name.endswith(".json")
        )
        if not analysis_shards:
            raise ValueError("Seed zip is missing cloudflare_seed/analysis_shards/*.json entries")

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

    if not isinstance(manifest, dict):
        raise ValueError("cloudflare_seed/manifest.json must be a JSON object")
    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("cloudflare_seed/manifest.json must contain a counts object")
    seed_companies = int(counts.get("companies") or 0)
    seed_analysis = int(counts.get("analysis") or 0)
    seed_universe = sum(int(counts.get(key) or 0) for key in ("entry", "watch", "excluded"))
    if seed_companies < MIN_SEED_COMPANIES:
        raise ValueError(f"Cloudflare seed has only {seed_companies} companies; expected at least {MIN_SEED_COMPANIES}")
    if seed_analysis < MIN_SEED_ANALYSIS:
        raise ValueError(f"Cloudflare seed has only {seed_analysis} analysis rows; expected at least {MIN_SEED_ANALYSIS}")
    if seed_universe < MIN_SEED_ANALYSIS:
        raise ValueError(f"Cloudflare seed universe has only {seed_universe} rows; expected at least {MIN_SEED_ANALYSIS}")

    return {
        "zip": str(path),
        "companies": company_count,
        "quarterlyRows": row_count,
        "latestPeriod": latest_period,
        "seedCompanies": seed_companies,
        "seedAnalysis": seed_analysis,
        "seedUniverse": seed_universe,
        "seedShards": len(analysis_shards),
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

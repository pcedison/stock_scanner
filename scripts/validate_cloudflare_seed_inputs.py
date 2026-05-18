from __future__ import annotations

import argparse
import csv
import json
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ZIP = Path("data/official_cache_seed_2026-05-14.zip")
DEFAULT_FAILED_COMPANIES_CSV = Path("data/official_history_failed_companies_2026Q1.csv")
REQUIRED_ENTRIES = {
    "official_fundamentals_history.json",
    "official_history_backfill_progress.json",
    "cloudflare_seed/manifest.json",
    "cloudflare_seed/companies.json",
    "cloudflare_seed/data_sources_status.json",
    "cloudflare_seed/market_scan_latest.json",
    "cloudflare_seed/analysis_by_code.json",
    "cloudflare_seed/holding_analysis_by_code.json",
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
        holding_analysis_shards = sorted(
            name
            for name in names
            if name.startswith("cloudflare_seed/holding_analysis_shards/") and name.endswith(".json")
        )
        if not analysis_shards:
            raise ValueError("Seed zip is missing cloudflare_seed/analysis_shards/*.json entries")
        if not holding_analysis_shards:
            raise ValueError("Seed zip is missing cloudflare_seed/holding_analysis_shards/*.json entries")

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
    seed_holding_analysis = int(counts.get("holdingAnalysis") or 0)
    seed_universe = sum(int(counts.get(key) or 0) for key in ("entry", "watch", "excluded"))
    if seed_companies < MIN_SEED_COMPANIES:
        raise ValueError(f"Cloudflare seed has only {seed_companies} companies; expected at least {MIN_SEED_COMPANIES}")
    if seed_analysis < MIN_SEED_ANALYSIS:
        raise ValueError(f"Cloudflare seed has only {seed_analysis} analysis rows; expected at least {MIN_SEED_ANALYSIS}")
    if seed_holding_analysis < MIN_SEED_ANALYSIS:
        raise ValueError(
            f"Cloudflare seed has only {seed_holding_analysis} holding analysis rows; expected at least {MIN_SEED_ANALYSIS}"
        )
    if seed_universe < MIN_SEED_ANALYSIS:
        raise ValueError(f"Cloudflare seed universe has only {seed_universe} rows; expected at least {MIN_SEED_ANALYSIS}")

    return {
        "zip": str(path),
        "companies": company_count,
        "quarterlyRows": row_count,
        "latestPeriod": latest_period,
        "seedCompanies": seed_companies,
        "seedAnalysis": seed_analysis,
        "seedHoldingAnalysis": seed_holding_analysis,
        "seedUniverse": seed_universe,
        "seedShards": len(analysis_shards),
        "seedHoldingShards": len(holding_analysis_shards),
        "generatedAt": manifest.get("generatedAt"),
        "sourceLastCheckedAt": manifest.get("sourceLastCheckedAt"),
        "latestRevenuePeriod": manifest.get("latestRevenuePeriod"),
        "latestFinancialPeriod": manifest.get("latestFinancialPeriod"),
        "requiredEntries": sorted(REQUIRED_ENTRIES),
    }


def validate_seed_freshness(summary: dict[str, Any], max_age_days: int | None, now: datetime | None = None) -> None:
    if max_age_days is None:
        return
    generated_at = summary.get("generatedAt")
    if not generated_at:
        raise ValueError("Seed manifest is missing generatedAt; cannot enforce freshness")
    try:
        generated = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Seed manifest generatedAt is not a valid ISO datetime: {generated_at}") from exc
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age_days = (current - generated).total_seconds() / 86400
    if age_days > max_age_days:
        raise ValueError(f"Seed manifest is {age_days:.1f} days old; maximum allowed is {max_age_days}")


def failed_company_summary(path: Path = DEFAULT_FAILED_COMPANIES_CSV) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "failedCompanies": 0, "manualStatus": {}, "reasons": {}}

    reasons: Counter[str] = Counter()
    manual_status: Counter[str] = Counter()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            reasons[row.get("initial_reason") or "unspecified"] += 1
            manual_status[row.get("manual_status") or row.get("subagent_status") or "todo"] += 1

    return {
        "path": str(path),
        "failedCompanies": sum(reasons.values()),
        "manualStatus": dict(sorted(manual_status.items())),
        "reasons": dict(reasons.most_common()),
    }


def render_seed_summary(summary: dict[str, Any], failed_summary: dict[str, Any] | None = None) -> str:
    failed_summary = failed_summary or failed_company_summary()
    lines = [
        "# Cloudflare seed quality",
        "",
        f"- zip: `{summary['zip']}`",
        f"- official history companies: {summary['companies']}",
        f"- official history quarterly rows: {summary['quarterlyRows']}",
        f"- latest official period: {summary['latestPeriod']}",
        f"- manifest generated at: {summary.get('generatedAt') or 'unknown'}",
        f"- latest revenue period: {summary.get('latestRevenuePeriod') or 'unknown'}",
        f"- latest financial period: {summary.get('latestFinancialPeriod') or 'unknown'}",
        f"- seed companies: {summary['seedCompanies']}",
        f"- seed analysis rows: {summary['seedAnalysis']}",
        f"- seed holding analysis rows: {summary['seedHoldingAnalysis']}",
        f"- seed universe rows: {summary['seedUniverse']}",
        f"- seed shards: {summary['seedShards']}",
        f"- seed holding shards: {summary['seedHoldingShards']}",
        f"- failed companies needing review: {failed_summary['failedCompanies']}",
        "",
        "## Manual follow-up status",
        "",
    ]
    manual_status = failed_summary.get("manualStatus") or {}
    if manual_status:
        lines.extend(f"- {status}: {count}" for status, count in manual_status.items())
    else:
        lines.append("- none")

    reasons = failed_summary.get("reasons") or {}
    if reasons:
        lines.extend(["", "## Missing-data reasons", ""])
        lines.extend(f"- {reason}: {count}" for reason, count in reasons.items())
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the committed Cloudflare seed cache inputs.")
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP, help="Path to the committed seed zip")
    parser.add_argument("--summary-md", type=Path, help="Optional Markdown quality summary output path")
    parser.add_argument("--summary-json", type=Path, help="Optional JSON quality summary output path")
    parser.add_argument("--max-age-days", type=int, help="Fail if cloudflare_seed/manifest.json generatedAt is older")
    parser.add_argument(
        "--failed-companies-csv",
        type=Path,
        default=DEFAULT_FAILED_COMPANIES_CSV,
        help="CSV used to summarize missing official history follow-up status",
    )
    args = parser.parse_args(argv)

    try:
        summary = validate_seed_zip(args.zip)
        validate_seed_freshness(summary, args.max_age_days)
    except ValueError as exc:
        print(f"Seed validation failed: {exc}", file=sys.stderr)
        return 1

    failed_summary = failed_company_summary(args.failed_companies_csv)
    if args.summary_md:
        args.summary_md.parent.mkdir(parents=True, exist_ok=True)
        args.summary_md.write_text(
            render_seed_summary(summary, failed_summary),
            encoding="utf-8",
        )
    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(
            json.dumps({"seed": summary, "failedCompanies": failed_summary}, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

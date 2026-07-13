from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from hydrate_cloudflare_seed_inputs import monthly_history_coverage  # noqa: E402
from seed_utils import find_seed_zip  # noqa: E402

DEFAULT_ZIP = find_seed_zip(Path("data"))
DEFAULT_FAILED_COMPANIES_CSV = Path("data/official_history_failed_companies_2026Q1.csv")
REQUIRED_ENTRIES = {
    "official_fundamentals_history.json",
    "official_history_backfill_progress.json",
    "monthly_revenue_history.json",
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
MIN_CONSECUTIVE_MONTHLY_COMPANIES = 1000
MARKET_SCAN_CATEGORIES = ("entry", "watch", "excluded")
LOCAL_USER_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/(?:Users|home)/)", re.IGNORECASE)


def _load_json_from_zip(archive: zipfile.ZipFile, name: str) -> Any:
    try:
        with archive.open(name) as handle:
            return json.loads(handle.read().decode("utf-8"))
    except KeyError as exc:
        raise ValueError(f"Missing required seed entry: {name}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Seed entry is not valid JSON: {name}") from exc


def _reject_local_user_paths(payload: Any, entry_name: str, location: str = "$") -> None:
    if isinstance(payload, str):
        if LOCAL_USER_PATH_RE.search(payload):
            raise ValueError(f"Public seed entry {entry_name} contains a local user path at {location}")
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(key, str) and LOCAL_USER_PATH_RE.search(key):
                raise ValueError(f"Public seed entry {entry_name} contains a local user path in an object key")
            _reject_local_user_paths(value, entry_name, f"{location}.{key}")
        return
    if isinstance(payload, list):
        for index, value in enumerate(payload):
            _reject_local_user_paths(value, entry_name, f"{location}[{index}]")


def _manifest_count(payload: dict[str, Any], key: str, label: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label}.{key} must be a non-negative integer")
    return value


def validate_seed_zip(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"Seed zip does not exist: {path}")

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        missing = sorted(REQUIRED_ENTRIES - names)
        if missing:
            raise ValueError(f"Seed zip is missing required entries: {', '.join(missing)}")

        history = _load_json_from_zip(archive, "official_fundamentals_history.json")
        monthly_history = _load_json_from_zip(archive, "monthly_revenue_history.json")
        _load_json_from_zip(archive, "official_history_backfill_progress.json")
        manifest = _load_json_from_zip(archive, "cloudflare_seed/manifest.json")
        market_scan = _load_json_from_zip(archive, "cloudflare_seed/market_scan_latest.json")
        loaded_public_payloads = {
            "cloudflare_seed/manifest.json": manifest,
            "cloudflare_seed/market_scan_latest.json": market_scan,
        }
        public_json_entries = sorted(
            name for name in names if name.startswith("cloudflare_seed/") and name.endswith(".json")
        )
        for name in public_json_entries:
            payload = loaded_public_payloads.get(name)
            if payload is None:
                payload = _load_json_from_zip(archive, name)
            _reject_local_user_paths(payload, name)
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

    if not isinstance(monthly_history, dict) or not isinstance(monthly_history.get("months"), dict):
        raise ValueError("monthly_revenue_history.json must contain a months object")
    monthly_coverage = monthly_history_coverage(monthly_history)
    if monthly_coverage["consecutiveCompanies"] < MIN_CONSECUTIVE_MONTHLY_COMPANIES:
        raise ValueError(
            "Seed has insufficient consecutive monthly revenue history: "
            f"{monthly_coverage['consecutiveCompanies']} companies cover "
            f"{monthly_coverage['previousMonth']} -> {monthly_coverage['latestMonth']}; "
            f"expected at least {MIN_CONSECUTIVE_MONTHLY_COMPANIES}"
        )

    if not isinstance(manifest, dict):
        raise ValueError("cloudflare_seed/manifest.json must be a JSON object")
    if not isinstance(market_scan, dict):
        raise ValueError("cloudflare_seed/market_scan_latest.json must be a JSON object")
    actual_category_counts: dict[str, int] = {}
    for category in MARKET_SCAN_CATEGORIES:
        rows = market_scan.get(category)
        if not isinstance(rows, list):
            raise ValueError(f"cloudflare_seed/market_scan_latest.json {category} must be a JSON list")
        actual_category_counts[category] = len(rows)
    actual_universe = sum(actual_category_counts.values())
    if actual_universe <= 0:
        raise ValueError("Cloudflare market scan has no rows")
    reported_universe = market_scan.get("universeSize")
    if reported_universe is not None:
        if isinstance(reported_universe, bool) or not isinstance(reported_universe, int):
            raise ValueError("Cloudflare market scan universeSize must be an integer")
        if reported_universe != actual_universe:
            raise ValueError(
                f"Cloudflare market scan universeSize {reported_universe} does not match actual total {actual_universe}"
            )

    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("cloudflare_seed/manifest.json must contain a counts object")
    seed_companies = _manifest_count(counts, "companies", "manifest counts")
    seed_analysis = _manifest_count(counts, "analysis", "manifest counts")
    seed_holding_analysis = _manifest_count(counts, "holdingAnalysis", "manifest counts")
    manifest_category_counts = {
        category: _manifest_count(counts, category, "manifest counts") for category in MARKET_SCAN_CATEGORIES
    }
    for category, actual_count in actual_category_counts.items():
        manifest_count = manifest_category_counts[category]
        if manifest_count != actual_count:
            raise ValueError(
                f"Cloudflare manifest counts.{category} {manifest_count} does not match market scan {actual_count}"
            )
    seed_universe = sum(manifest_category_counts.values())

    optional_category_counts = manifest.get("categoryCounts")
    if optional_category_counts is not None:
        if not isinstance(optional_category_counts, dict):
            raise ValueError("cloudflare_seed/manifest.json categoryCounts must be a JSON object")
        for category, actual_count in actual_category_counts.items():
            manifest_count = _manifest_count(optional_category_counts, category, "manifest categoryCounts")
            if manifest_count != actual_count:
                raise ValueError(
                    f"Cloudflare manifest categoryCounts.{category} {manifest_count} "
                    f"does not match market scan {actual_count}"
                )

    manifest_universe = manifest.get("universeSize")
    if manifest_universe is not None:
        if isinstance(manifest_universe, bool) or not isinstance(manifest_universe, int):
            raise ValueError("Cloudflare manifest universeSize must be an integer")
        if manifest_universe != actual_universe:
            raise ValueError(
                f"Cloudflare manifest universeSize {manifest_universe} does not match market scan {actual_universe}"
            )
    if seed_companies < MIN_SEED_COMPANIES:
        raise ValueError(f"Cloudflare seed has only {seed_companies} companies; expected at least {MIN_SEED_COMPANIES}")
    if seed_analysis < MIN_SEED_ANALYSIS:
        raise ValueError(
            f"Cloudflare seed has only {seed_analysis} analysis rows; expected at least {MIN_SEED_ANALYSIS}"
        )
    if seed_holding_analysis < MIN_SEED_ANALYSIS:
        raise ValueError(
            f"Cloudflare seed has only {seed_holding_analysis} holding analysis rows; expected at least {MIN_SEED_ANALYSIS}"
        )
    if seed_universe < MIN_SEED_ANALYSIS:
        raise ValueError(
            f"Cloudflare seed universe has only {seed_universe} rows; expected at least {MIN_SEED_ANALYSIS}"
        )

    return {
        "zip": str(path),
        "companies": company_count,
        "quarterlyRows": row_count,
        "latestPeriod": latest_period,
        "latestRevenueHistoryMonth": monthly_coverage["latestMonth"],
        "previousRevenueHistoryMonth": monthly_coverage["previousMonth"],
        "consecutiveRevenueHistoryCompanies": monthly_coverage["consecutiveCompanies"],
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
        generated = generated.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
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
        f"- latest monthly revenue history: {summary.get('latestRevenueHistoryMonth') or 'unknown'}",
        f"- consecutive monthly revenue coverage: {summary.get('consecutiveRevenueHistoryCompanies') or 0}",
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
            json.dumps(
                {"seed": summary, "failedCompanies": failed_summary}, ensure_ascii=False, indent=2, sort_keys=True
            ),
            encoding="utf-8",
        )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

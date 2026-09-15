from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from fastapi.encoders import jsonable_encoder

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
OUT_DIR = ROOT_DIR / "cloudflare" / "seed"
ANALYSIS_SHARD_DIR = OUT_DIR / "analysis_shards"
HOLDING_ANALYSIS_SHARD_DIR = OUT_DIR / "holding_analysis_shards"
OFFLINE_SEED_PREFIX = "cloudflare_seed/"
OFFLINE_SEED_REQUIRED_FILES = {
    "manifest.json",
    "companies.json",
    "data_sources_status.json",
    "market_scan_latest.json",
    "analysis_by_code.json",
    "holding_analysis_by_code.json",
}

# Ensure both project root (for backend.*) and scripts/ (for seed_utils) are importable.
for _p in (str(ROOT_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from seed_utils import find_seed_zip  # noqa: E402

SEED_CACHE_ZIP = find_seed_zip(ROOT_DIR / "data")

from backend.models.company import Company  # noqa: E402
from backend.models.financial import FundamentalSnapshot  # noqa: E402
from backend.models.holding import Holding  # noqa: E402
from backend.models.settings import ScannerSettings  # noqa: E402
from backend.services.cache_policy import next_refresh_after, refresh_policy  # noqa: E402
from backend.services.calendar import load_market_calendar  # noqa: E402
from backend.services.market_query import build_market_generation, canonical_json_bytes  # noqa: E402
from backend.services.market_scan import data_sources_status_payload, scan_market_payload  # noqa: E402
from backend.services.official_data_provider import OfficialDataProvider, _is_financial_company  # noqa: E402
from backend.services.report_render import render_market_report_csv, render_market_report_markdown  # noqa: E402
from backend.services.rules import RuleEngine  # noqa: E402
from backend.services.settings_service import load_settings  # noqa: E402

engine = RuleEngine()
official_provider = OfficialDataProvider()


def _scan_market_payload(settings: ScannerSettings) -> dict:
    return scan_market_payload(settings, official_provider, engine)


def _data_sources_status_payload() -> dict:
    settings = load_settings()
    settings.use_mock_data = False
    return data_sources_status_payload(settings, official_provider)


MIN_SEED_COMPANY_SIZE = int(os.getenv("MIN_SEED_COMPANY_SIZE", "1000"))
MIN_SEED_TWSE_COMPANIES = int(os.getenv("MIN_SEED_TWSE_COMPANIES", "1000"))
MIN_SEED_TPEX_COMPANIES = int(os.getenv("MIN_SEED_TPEX_COMPANIES", "700"))
MIN_SEED_UNIVERSE_SIZE = int(os.getenv("MIN_SEED_UNIVERSE_SIZE", "1000"))
MIN_SEED_ANALYSIS_SIZE = int(os.getenv("MIN_SEED_ANALYSIS_SIZE", "1000"))
MAX_X2_MISSING_RATIO = min(1.0, max(0.0, float(os.getenv("MAX_X2_MISSING_RATIO", "0.10"))))
MARKET_REPORT_CSV = "reports/market_scan.csv"
MARKET_REPORT_MD = "reports/market_scan.md"
# Retired when the Worker started streaming the report exports; zips built before that still
# ship the member and declare it in their manifest, so the offline copy drops both.
RETIRED_SEED_FILE = "market_scan_summary.json"
MARKET_REPORT_TITLE = "台股市場掃描報告"
MARKET_SCAN_CATEGORIES = ("entry", "watch", "excluded", "results")
SUMMARY_RESULT_KEYS = ("stockCode", "companyName", "status", "summary")
REQUIRED_SCAN_SUMMARY_KEYS = ("stockCode", "companyName", "status")
SUMMARY_REASON_KEYS = ("code", "title", "passed", "severity", "message")
SUMMARY_REASON_CODES = {"E4", "OFFICIAL_Q", "OFFICIAL_VALUATION", "X1", "X2", "X3", "X4", "X5"}
OFFICIAL_MANIFEST_FILES = (
    "official_fundamentals_history.json",
    "official_history_backfill_progress.json",
    "monthly_revenue_history.json",
)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jsonable_encoder(payload), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _resolved_seed_child(path: Path) -> Path:
    output_root = OUT_DIR.resolve()
    target = path.resolve()
    if target == output_root or not target.is_relative_to(output_root):
        raise RuntimeError("Refusing to access generated output outside the seed directory")
    return target


def _remove_generated_json_files(directories: list[Path]) -> None:
    for directory in directories:
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            path.unlink()


def clear_generated_analysis_shards() -> None:
    directories = [_resolved_seed_child(path) for path in (ANALYSIS_SHARD_DIR, HOLDING_ANALYSIS_SHARD_DIR)]
    _remove_generated_json_files(directories)



def market_closed_dates(year: int) -> set:
    """TWSE closed days for this year and the next, so the walk forward can cross 31 Dec."""
    closed: set = set()
    for value in (year, year + 1):
        closed |= load_market_calendar(value).closed_dates
    return closed

def clear_seed_output() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    analysis_dirs = [_resolved_seed_child(path) for path in (ANALYSIS_SHARD_DIR, HOLDING_ANALYSIS_SHARD_DIR)]
    market_v2_dir = _resolved_seed_child(OUT_DIR / "market_scan" / "v2")
    for path in OUT_DIR.glob("*.json"):
        path.unlink()
    _remove_generated_json_files(analysis_dirs)
    if market_v2_dir.exists():
        shutil.rmtree(market_v2_dir)


def analysis_shard_key(stock_code: str) -> str:
    return str(stock_code)[:2]


def compact_scan_result(result: dict) -> dict:
    if not isinstance(result, dict):
        result = jsonable_encoder(result)
    if not isinstance(result, dict):
        return {}
    compact = {key: result.get(key) for key in SUMMARY_RESULT_KEYS if key in result}
    reasons = result.get("reasons")
    if isinstance(reasons, list):
        compact["reasons"] = []
        for reason in reasons:
            if not isinstance(reason, dict):
                continue
            if str(reason.get("code") or "") not in SUMMARY_REASON_CODES:
                continue
            cast(list, compact["reasons"]).append(
                {key: reason.get(key) for key in SUMMARY_REASON_KEYS if key in reason}
            )
    compact["detailsAvailable"] = True
    compact["hasFullDetails"] = False
    return compact


def compact_market_scan_payload(scan_payload: dict) -> dict:
    if not isinstance(scan_payload, dict):
        return {}
    compact = dict(scan_payload)
    for category in MARKET_SCAN_CATEGORIES:
        items = compact.get(category)
        if isinstance(items, list):
            compact[category] = [compact_scan_result(item) for item in items]
    compact["detailMode"] = "summary"
    return compact


def add_market_reports_to_manifest(manifest: dict) -> dict:
    """Declare the static report exports in a manifest copied from an older seed."""
    updated = dict(manifest)
    files = list(updated.get("files") or [])
    for name in (MARKET_REPORT_CSV, MARKET_REPORT_MD):
        if name not in files:
            files.append(name)
    updated["files"] = files
    return updated


def write_market_reports(scan_payload: dict) -> None:
    """Static market report exports: the Worker streams these instead of decoding the scan."""
    compact = compact_market_scan_payload(scan_payload)
    _atomic_write_bytes(OUT_DIR / MARKET_REPORT_CSV, render_market_report_csv(compact).encode("utf-8"))
    _atomic_write_bytes(OUT_DIR / MARKET_REPORT_MD, render_market_report_markdown(compact, MARKET_REPORT_TITLE).encode("utf-8"))


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def write_market_generation(scan_payload: dict, manifest: dict) -> dict:
    filing_context = scan_payload.get("filingContext")
    if not isinstance(filing_context, dict):
        filing_context = {}
    generation = build_market_generation(
        compact_market_scan_payload(scan_payload),
        cache_status_inputs={
            "generatedAt": scan_payload.get("generatedAt"),
            "latestRevenuePeriod": filing_context.get("monthlyRevenuePeriod"),
            "latestFinancialPeriod": latest_financial_period(scan_payload),
        },
    )
    generation_index_key = f"public/market_scan/v2/{generation.generation_id}/index.json"
    page_sizes = []
    immutable_files = []
    for object_key, content in generation.files.items():
        expected_prefix = f"public/market_scan/v2/{generation.generation_id}/"
        if not object_key.startswith(expected_prefix):
            raise RuntimeError("Refusing to write an unsafe market generation key")
        target = _resolved_seed_child(OUT_DIR / object_key.removeprefix("public/"))
        immutable_files.append((object_key, target, content))
        if object_key != generation_index_key:
            page_sizes.append(len(content))
    for _object_key, target, content in immutable_files:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _atomic_write_bytes(OUT_DIR / "market_scan_index.json", canonical_json_bytes(generation.index))

    updated = dict(manifest)
    files = list(updated.get("files") or [])
    if "market_scan_index.json" not in files:
        files.append("market_scan_index.json")
    updated["files"] = files
    updated["marketApiSchemaVersion"] = 2
    updated["marketGenerationId"] = generation.generation_id
    updated["marketIndexBytes"] = len(canonical_json_bytes(generation.index))
    updated["marketPageCount"] = len(page_sizes)
    updated["marketMaxPageBytes"] = max(page_sizes, default=0)
    return updated


def rebuild_scan_from_analysis(scan_payload: dict, results: list[dict]) -> dict:
    rebuilt = dict(scan_payload)
    rebuilt["entry"] = [item for item in results if item.get("status") == "ENTRY"]
    rebuilt["excluded"] = [item for item in results if item.get("status") == "EXCLUDED"]
    rebuilt["watch"] = [item for item in results if item.get("status") not in {"ENTRY", "EXCLUDED"}]
    rebuilt["universeSize"] = len(rebuilt["entry"]) + len(rebuilt["watch"]) + len(rebuilt["excluded"])
    return rebuilt


def scan_payload_needs_rebuild(scan_payload: dict) -> bool:
    if not scan_payload.get("universeSize"):
        return True
    for category in ("entry", "watch", "excluded"):
        items = scan_payload.get(category)
        if not isinstance(items, list) or not items:
            continue
        first_item = items[0] if isinstance(items[0], dict) else jsonable_encoder(items[0])
        if not isinstance(first_item, dict):
            return True
        return any(not first_item.get(key) for key in REQUIRED_SCAN_SUMMARY_KEYS)
    return False


def period_key(period: str | None) -> tuple[int, int]:
    try:
        year, quarter = str(period or "").upper().split("Q", 1)
        return int(year), int(quarter)
    except (AttributeError, ValueError):
        return 0, 0


def month_key(month: str | None) -> tuple[int, int]:
    try:
        year, month_number = str(month or "").split("-", 1)
        return int(year), int(month_number)
    except (AttributeError, ValueError):
        return 0, 0


def latest_monthly_history_record(months_by_code: dict, stock_code: str) -> tuple[str | None, dict | None]:
    company_months = months_by_code.get(stock_code)
    if not isinstance(company_months, dict):
        return None, None
    valid_months = [
        (month, record)
        for month, record in company_months.items()
        if isinstance(month, str) and isinstance(record, dict) and month_key(month) != (0, 0)
    ]
    if not valid_months:
        return None, None
    return max(valid_months, key=lambda item: month_key(item[0]))


def history_yoy(records: dict, record: dict, field: str) -> float | None:
    fiscal_year = record.get("fiscalYear")
    quarter = record.get("quarter")
    current = record.get(field)
    if fiscal_year is None or quarter is None or current is None:
        return None
    previous = records.get(f"{int(fiscal_year) - 1}Q{int(quarter)}")
    if not isinstance(previous, dict):
        return None
    previous_value = previous.get(field)
    if previous_value is None or previous_value == 0:
        return None
    return ((float(current) - float(previous_value)) / abs(float(previous_value))) * 100


def history_margin_delta(records: dict, record: dict, field: str) -> float | None:
    fiscal_year = record.get("fiscalYear")
    quarter = record.get("quarter")
    current = record.get(field)
    if fiscal_year is None or quarter is None or current is None:
        return None
    previous = records.get(f"{int(fiscal_year) - 1}Q{int(quarter)}")
    if not isinstance(previous, dict) or previous.get(field) is None:
        return None
    return float(current) - float(previous[field])


def annual_financials_from_history(records: dict) -> list[dict]:
    annuals = []
    for record in records.values():
        if not isinstance(record, dict) or record.get("quarter") != 4 or record.get("fiscalYear") is None:
            continue
        annuals.append({"year": int(record["fiscalYear"]), "netIncome": record.get("netIncome")})
    return list({row["year"]: row for row in sorted(annuals, key=lambda item: cast(int, item["year"]))}.values())


def inventory_turnover_from_history(record: dict) -> float | None:
    cost = record.get("costOfRevenue")
    inventory = record.get("inventory")
    quarter = record.get("quarter")
    if cost is None or inventory is None or inventory == 0 or not quarter:
        return None
    return (float(cost) * (4 / int(quarter))) / float(inventory)


def cached_seed_company_lookup(path: Path | None = None) -> dict[str, Company]:
    """Load stable company metadata when live profile endpoints are unavailable."""
    path = path or SEED_CACHE_ZIP
    if not path.exists():
        return {}
    try:
        with zipfile.ZipFile(path) as archive:
            payload = json.loads(archive.read(f"{OFFLINE_SEED_PREFIX}companies.json").decode("utf-8"))
    except (OSError, KeyError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile):
        return {}
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {}
    companies: dict[str, Company] = {}
    for item in items:
        try:
            company = Company.model_validate(item)
        except (TypeError, ValueError):
            continue
        companies[company.stockCode] = company
    return companies


def history_seed_snapshots(settings) -> list[FundamentalSnapshot]:
    # Preserve market/name metadata from the deterministic seed when either
    # official company-profile endpoint is temporarily unavailable.
    company_lookup = cached_seed_company_lookup()
    company_lookup.update({c.stockCode: c for c in official_provider.list_companies()})

    payload = official_provider.history_store.load()
    quarters = payload.get("quarters", {})
    if not isinstance(quarters, dict):
        return []
    monthly_payload = official_provider.monthly_revenue_history.load()
    months_by_code = monthly_payload.get("months", {}) if isinstance(monthly_payload, dict) else {}
    if not isinstance(months_by_code, dict):
        months_by_code = {}

    snapshots: list[FundamentalSnapshot] = []
    for stock_code, records in quarters.items():
        if not isinstance(stock_code, str) or not stock_code.isdigit() or not isinstance(records, dict) or not records:
            continue
        latest_period = max(records, key=period_key)
        record = records.get(latest_period)
        if not isinstance(record, dict):
            continue

        existing = company_lookup.get(stock_code)
        if existing:
            market = existing.market
            company_name = existing.name
            industry_name = existing.industryName
            is_financial = existing.isFinancial
        else:
            market = (
                cast(Literal["TWSE", "TPEX", "OTHER"], record.get("market"))
                if record.get("market") in {"TWSE", "TPEX"}
                else "OTHER"
            )
            company_name = str(record.get("companyName") or stock_code)
            industry_name = "Unknown industry"
            is_financial = _is_financial_company(stock_code, industry_name, company_name)

        if market == "TWSE" and not settings.scan_twse:
            continue
        if market == "TPEX" and not settings.scan_tpex:
            continue
        fiscal_year = int(record.get("fiscalYear") or period_key(latest_period)[0] or 1970)
        quarter_month = max(1, min(12, int(record.get("quarter") or 1) * 3))
        fallback_month = f"{fiscal_year}-{quarter_month:02d}"
        monthly_month, monthly_record = latest_monthly_history_record(months_by_code, stock_code)
        snapshot_month = monthly_month or fallback_month
        try:
            snapshot_year = int(snapshot_month[:4])
        except (ValueError, TypeError):
            snapshot_year = 0
        company = Company(
            stockCode=stock_code,
            name=company_name,
            market=market,
            industryName=industry_name,
            isFinancial=is_financial,
        )
        snapshots.append(
            FundamentalSnapshot.model_validate(
                {
                    "company": company.model_dump(),
                    "monthlyRevenue": {
                        "month": snapshot_month,
                        "monthlyRevenueYoY": monthly_record.get("monthlyRevenueYoY") if monthly_record else None,
                        "previousMonthRevenueYoY": official_provider.monthly_revenue_history.previous_month_yoy(
                            stock_code, snapshot_month
                        ),
                        "cumulativeRevenueYoY": monthly_record.get("cumulativeRevenueYoY") if monthly_record else None,
                        "trailingThreeMonthAverageYoY": official_provider.monthly_revenue_history.trailing_three_month_avg_yoy(
                            stock_code, snapshot_month
                        ),
                        "janFebCombinedRevenueYoY": official_provider.monthly_revenue_history.jan_feb_combined_yoy(
                            stock_code, snapshot_year
                        )
                        if snapshot_year
                        else None,
                        "isSpringFestivalMonth": False,
                    },
                    "quarterlyFinancial": {
                        "quarter": latest_period,
                        "eps": record.get("eps"),
                        "epsYoY": history_yoy(records, record, "eps"),
                        "netIncome": record.get("netIncome"),
                        "netIncomeYoY": history_yoy(records, record, "netIncome"),
                        "revenue": record.get("revenue"),
                        "grossMargin": record.get("grossMargin"),
                        "grossMarginYoY": history_margin_delta(records, record, "grossMargin"),
                        "operatingMargin": record.get("operatingMargin"),
                    },
                    "valuation": {
                        "per": None,
                        "priceBookRatio": None,
                        "dividendYield": None,
                        "valuationDate": None,
                        "valuationFiscalQuarter": latest_period,
                        "inventoryTurnover": inventory_turnover_from_history(record),
                        "roe": None,
                        "nonPerformingLoanRatio": None,
                        "capitalAdequacyRatio": None,
                        "netInterestMargin": None,
                    },
                    "annualFinancials": annual_financials_from_history(records),
                }
            )
        )
    return sorted(snapshots, key=lambda snapshot: snapshot.company.stockCode)


def seed_diagnostics(scan_payload: dict, companies: list, analysis_by_code: dict, fallback_source: str | None) -> dict:
    universe_size = scan_payload.get("universeSize") or 0
    x2_missing = 0
    for category in ("entry", "watch", "excluded"):
        for result in scan_payload.get(category, []):
            encoded_result = result if isinstance(result, dict) else jsonable_encoder(result)
            if not isinstance(encoded_result, dict):
                continue
            reasons = encoded_result.get("reasons")
            if not isinstance(reasons, list):
                continue
            for reason in reasons:
                encoded_reason = reason if isinstance(reason, dict) else jsonable_encoder(reason)
                if (
                    isinstance(encoded_reason, dict)
                    and encoded_reason.get("code") == "X2"
                    and encoded_reason.get("severity") == "INSUFFICIENT_DATA"
                ):
                    x2_missing += 1
                    break
    return {
        "universeSize": universe_size,
        "companies": len(companies),
        "companiesByMarket": company_market_counts(companies),
        "analysis": len(analysis_by_code),
        "entry": len(scan_payload.get("entry", [])),
        "watch": len(scan_payload.get("watch", [])),
        "excluded": len(scan_payload.get("excluded", [])),
        "x2Missing": x2_missing,
        "x2MissingRatio": x2_missing / universe_size if universe_size else 0.0,
        "fallbackSource": fallback_source,
        "financialFreshness": scan_payload.get("financialFreshness"),
        "providerStatus": official_provider.status(refresh=False),
    }


def assert_seed_quality(
    scan_payload: dict, companies: list, analysis_by_code: dict, fallback_source: str | None
) -> None:
    diagnostics = seed_diagnostics(scan_payload, companies, analysis_by_code, fallback_source)
    if diagnostics["companies"] < MIN_SEED_COMPANY_SIZE:
        raise RuntimeError(
            "Refusing to publish an undersized company seed: "
            + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
        )
    companies_by_market = diagnostics["companiesByMarket"]
    for market, minimum in (
        ("TWSE", MIN_SEED_TWSE_COMPANIES),
        ("TPEX", MIN_SEED_TPEX_COMPANIES),
    ):
        if companies_by_market[market] < minimum:
            raise RuntimeError(
                f"Refusing to publish undersized {market} company market coverage: "
                + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
            )
    if diagnostics["universeSize"] < MIN_SEED_UNIVERSE_SIZE:
        raise RuntimeError(
            "Refusing to publish an undersized market scan seed: "
            + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
        )
    if diagnostics["analysis"] < MIN_SEED_ANALYSIS_SIZE:
        raise RuntimeError(
            "Refusing to publish an undersized analysis seed: "
            + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
        )
    if fallback_source and diagnostics["entry"] <= 0:
        raise RuntimeError(
            "Refusing to publish a zero-entry fallback seed: "
            + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
        )
    if diagnostics["x2MissingRatio"] > MAX_X2_MISSING_RATIO:
        raise RuntimeError(
            "Refusing to publish insufficient X2 monthly-history coverage: "
            + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
        )
    freshness = diagnostics.get("financialFreshness")
    if isinstance(freshness, dict) and freshness.get("blocksDeployment") is True:
        raise RuntimeError(
            "Refusing to publish a stale financial freshness seed: "
            + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
        )


def latest_financial_period(scan_payload: dict) -> str | None:
    freshness = scan_payload.get("financialFreshness")
    if isinstance(freshness, dict):
        cached = freshness.get("latestCachedFinancialPeriod")
        if isinstance(cached, str) and cached:
            return cached
    context = scan_payload.get("filingContext")
    if not isinstance(context, dict):
        return None
    for field in ("freshnessFinancialReport", "activeFinancialReport"):
        report = context.get(field)
        period = report.get("period") if isinstance(report, dict) else None
        if isinstance(period, str) and period:
            return period
    return None


def has_offline_seed_payload(path: Path = SEED_CACHE_ZIP) -> bool:
    if not path.exists():
        return False
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    has_required_files = all(f"{OFFLINE_SEED_PREFIX}{name}" in names for name in OFFLINE_SEED_REQUIRED_FILES)
    has_analysis_shards = any(
        name.startswith(f"{OFFLINE_SEED_PREFIX}analysis_shards/") and name.endswith(".json") for name in names
    )
    has_holding_analysis_shards = any(
        name.startswith(f"{OFFLINE_SEED_PREFIX}holding_analysis_shards/") and name.endswith(".json") for name in names
    )
    return has_required_files and has_analysis_shards and has_holding_analysis_shards


def _drop_retired_manifest_files(manifest: dict) -> dict:
    """Drop entries an older seed's manifest still declares but this builder no longer writes."""
    updated = dict(manifest)
    updated["files"] = [name for name in (updated.get("files") or []) if name != RETIRED_SEED_FILE]
    return updated


def copy_offline_seed_payload(path: Path = SEED_CACHE_ZIP) -> dict:
    clear_seed_output()
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        missing = sorted(
            f"{OFFLINE_SEED_PREFIX}{name}"
            for name in OFFLINE_SEED_REQUIRED_FILES
            if f"{OFFLINE_SEED_PREFIX}{name}" not in names
        )
        if missing:
            raise RuntimeError(f"Offline seed cache is missing required entries: {', '.join(missing)}")

        for name in names:
            if not name.startswith(OFFLINE_SEED_PREFIX) or name.endswith("/"):
                continue
            if name == f"{OFFLINE_SEED_PREFIX}{RETIRED_SEED_FILE}":
                continue
            relative = Path(name.removeprefix(OFFLINE_SEED_PREFIX))
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(f"Unsafe offline seed entry path: {name}")
            target = OUT_DIR / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))

    manifest = json.loads((OUT_DIR / "manifest.json").read_text(encoding="utf-8"))
    scan_payload = json.loads((OUT_DIR / "market_scan_latest.json").read_text(encoding="utf-8"))
    companies_payload = json.loads((OUT_DIR / "companies.json").read_text(encoding="utf-8"))
    companies = companies_payload.get("items") if isinstance(companies_payload, dict) else None
    if not isinstance(companies, list):
        raise RuntimeError("Offline seed companies.json must contain an items list")
    write_market_reports(scan_payload)
    manifest = _drop_retired_manifest_files(manifest)
    manifest = add_market_reports_to_manifest(manifest)
    manifest = write_market_generation(scan_payload, manifest)
    manifest["companiesByMarket"] = company_market_counts(companies)
    counts = manifest.get("counts", {})
    assert_seed_quality(
        {
            "universeSize": sum(int(counts.get(key) or 0) for key in ("entry", "watch", "excluded")),
            "entry": [None] * int(counts.get("entry") or 0),
            "watch": [None] * int(counts.get("watch") or 0),
            "excluded": [None] * int(counts.get("excluded") or 0),
        },
        companies,
        {str(index): None for index in range(int(counts.get("analysis") or 0))},
        "cloudflare_seed_cache",
    )
    return manifest


def _build_analysis_for_snapshots(
    snapshots,
    settings,
    analysis_by_code: dict,
    analysis_shards: dict,
    holding_analysis_by_code: dict,
    holding_analysis_shards: dict,
    analysis_results: list,
) -> None:
    for snapshot in snapshots:
        result = engine.evaluate_entry(snapshot, settings)
        encoded = jsonable_encoder(result)
        analysis_by_code[result.stockCode] = encoded
        analysis_shards.setdefault(analysis_shard_key(result.stockCode), {})[result.stockCode] = encoded
        analysis_results.append(encoded)
        holding = Holding(stockCode=result.stockCode, name=result.companyName, shares=0, averageCost=None)
        holding_result = engine.evaluate_holding(snapshot, holding, settings)
        holding_encoded = jsonable_encoder(holding_result)
        holding_analysis_by_code[result.stockCode] = holding_encoded
        holding_analysis_shards.setdefault(analysis_shard_key(result.stockCode), {})[result.stockCode] = holding_encoded


def merge_seed_companies(companies: list[Company], snapshots: list[FundamentalSnapshot]) -> list[Company]:
    """Keep profile-only companies while filling any profile refresh gaps from snapshots."""
    by_code = {snapshot.company.stockCode: snapshot.company for snapshot in snapshots}
    by_code.update({company.stockCode: company for company in companies})
    return [by_code[stock_code] for stock_code in sorted(by_code)]


def company_market_counts(companies: list) -> dict[str, int]:
    counts = {"TWSE": 0, "TPEX": 0}
    for company in companies:
        market = company.get("market") if isinstance(company, dict) else getattr(company, "market", None)
        if market in counts:
            counts[market] += 1
    return counts


def main() -> None:
    seed_mode = os.getenv("CLOUDFLARE_SEED_MODE", "offline_first").strip().lower()
    if seed_mode in {"offline", "offline_first"}:
        if has_offline_seed_payload(SEED_CACHE_ZIP):
            manifest = copy_offline_seed_payload(SEED_CACHE_ZIP)
            manifest["qualityGates"] = {
                **manifest.get("qualityGates", {}),
                "buildMode": "offline",
                "sourceZip": SEED_CACHE_ZIP.relative_to(ROOT_DIR).as_posix(),
            }
            write_json(OUT_DIR / "manifest.json", manifest)
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return
        if seed_mode == "offline":
            raise RuntimeError(f"{SEED_CACHE_ZIP} does not contain a complete cloudflare_seed payload")

    settings = load_settings()
    settings.use_mock_data = False
    settings.manual_scan_enabled = True

    clear_seed_output()
    scan_payload = _scan_market_payload(settings)
    policy = refresh_policy()
    generated_at = datetime.fromisoformat(scan_payload["generatedAt"])
    # The seed stays fresh until the next publication slot (a trading day's official
    # dataset regeneration); rebuilding any earlier only re-fetches identical data. The
    # Worker reads this value from the manifest instead of re-deriving it.
    closed_dates = market_closed_dates(generated_at.year)
    next_refresh = next_refresh_after(generated_at, closed_dates)
    snapshots = official_provider.list_snapshots(settings)
    companies = merge_seed_companies(official_provider.list_companies(), snapshots)
    analysis_by_code: dict = {}
    analysis_shards: dict[str, dict[str, dict]] = {}
    holding_analysis_by_code: dict = {}
    holding_analysis_shards: dict[str, dict[str, dict]] = {}
    analysis_results: list = []
    fallback_source = None

    _build_analysis_for_snapshots(
        snapshots,
        settings,
        analysis_by_code,
        analysis_shards,
        holding_analysis_by_code,
        holding_analysis_shards,
        analysis_results,
    )

    if not analysis_results:
        history_snapshots = history_seed_snapshots(settings)
        if history_snapshots:
            fallback_source = "official_fundamentals_history"
            companies = [snapshot.company for snapshot in history_snapshots]
            _build_analysis_for_snapshots(
                history_snapshots,
                settings,
                analysis_by_code,
                analysis_shards,
                holding_analysis_by_code,
                holding_analysis_shards,
                analysis_results,
            )

    if analysis_results and scan_payload_needs_rebuild(scan_payload):
        scan_payload = rebuild_scan_from_analysis(scan_payload, analysis_results)

    assert_seed_quality(scan_payload, companies, analysis_by_code, fallback_source)

    write_json(OUT_DIR / "market_scan_latest.json", scan_payload)
    write_market_reports(scan_payload)
    write_json(OUT_DIR / "companies.json", {"items": companies})
    write_json(OUT_DIR / "analysis_by_code.json", analysis_by_code)
    write_json(OUT_DIR / "holding_analysis_by_code.json", holding_analysis_by_code)
    for shard_key, shard_payload in analysis_shards.items():
        write_json(ANALYSIS_SHARD_DIR / f"{shard_key}.json", shard_payload)
    for shard_key, shard_payload in holding_analysis_shards.items():
        write_json(HOLDING_ANALYSIS_SHARD_DIR / f"{shard_key}.json", shard_payload)
    write_json(OUT_DIR / "data_sources_status.json", _data_sources_status_payload())

    manifest = {
        "generatedAt": scan_payload.get("generatedAt"),
        "sourceLastCheckedAt": scan_payload.get("generatedAt"),
        "nextRefreshAfter": next_refresh.isoformat(),
        "latestRevenuePeriod": scan_payload.get("filingContext", {}).get("monthlyRevenuePeriod"),
        "latestFinancialPeriod": latest_financial_period(scan_payload),
        "companiesByMarket": company_market_counts(companies),
        "financialFreshness": scan_payload.get("financialFreshness"),
        "cachePolicy": policy,
        # The Worker applies the same trading-day rule and has no access to the repo
        # calendar, so the closed days ride along with every rebuild.
        "marketClosedDates": sorted(day.isoformat() for day in closed_dates),
        "files": [
            "market_scan_latest.json",
            MARKET_REPORT_CSV,
            MARKET_REPORT_MD,
            "companies.json",
            "analysis_by_code.json",
            "analysis_shards/*.json",
            "holding_analysis_by_code.json",
            "holding_analysis_shards/*.json",
            "data_sources_status.json",
            *OFFICIAL_MANIFEST_FILES,
            *(([SEED_CACHE_ZIP.name]) if SEED_CACHE_ZIP.exists() else []),
        ],
        "counts": {
            "companies": len(companies),
            "entry": len(scan_payload.get("entry", [])),
            "watch": len(scan_payload.get("watch", [])),
            "excluded": len(scan_payload.get("excluded", [])),
            "analysis": len(analysis_by_code),
            "analysisShards": len(analysis_shards),
            "holdingAnalysis": len(holding_analysis_by_code),
            "holdingAnalysisShards": len(holding_analysis_shards),
        },
        "qualityGates": {
            "minimumCompanySize": MIN_SEED_COMPANY_SIZE,
            "minimumCompaniesByMarket": {
                "TWSE": MIN_SEED_TWSE_COMPANIES,
                "TPEX": MIN_SEED_TPEX_COMPANIES,
            },
            "minimumUniverseSize": MIN_SEED_UNIVERSE_SIZE,
            "minimumAnalysisSize": MIN_SEED_ANALYSIS_SIZE,
            "maximumX2MissingRatio": MAX_X2_MISSING_RATIO,
            "x2Missing": seed_diagnostics(scan_payload, companies, analysis_by_code, fallback_source)["x2Missing"],
            "fallbackSource": fallback_source,
        },
    }
    manifest = write_market_generation(scan_payload, manifest)
    write_json(OUT_DIR / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

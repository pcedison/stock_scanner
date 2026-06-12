from __future__ import annotations

import json
import os
import sys
import zipfile
from datetime import datetime, timedelta
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
from backend.services.cache_policy import refresh_policy  # noqa: E402
from backend.services.market_scan import data_sources_status_payload, scan_market_payload  # noqa: E402
from backend.services.official_data_provider import OfficialDataProvider, _is_financial_company  # noqa: E402
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
MIN_SEED_UNIVERSE_SIZE = int(os.getenv("MIN_SEED_UNIVERSE_SIZE", "1000"))
MIN_SEED_ANALYSIS_SIZE = int(os.getenv("MIN_SEED_ANALYSIS_SIZE", "1000"))
MARKET_SCAN_SUMMARY_FILE = "market_scan_summary.json"
MARKET_SCAN_CATEGORIES = ("entry", "watch", "excluded", "results")
SUMMARY_RESULT_KEYS = ("stockCode", "companyName", "status", "summary")
REQUIRED_SCAN_SUMMARY_KEYS = ("stockCode", "companyName", "status")
SUMMARY_REASON_KEYS = ("code", "title", "passed", "severity", "message")
SUMMARY_REASON_CODES = {"E4", "OFFICIAL_Q", "OFFICIAL_VALUATION", "X1", "X2", "X3", "X4", "X5"}


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jsonable_encoder(payload), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def clear_generated_analysis_shards() -> None:
    for directory in (ANALYSIS_SHARD_DIR, HOLDING_ANALYSIS_SHARD_DIR):
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            path.unlink()


def clear_seed_output() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in OUT_DIR.glob("*.json"):
        path.unlink()
    clear_generated_analysis_shards()


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
            cast(list, compact["reasons"]).append({key: reason.get(key) for key in SUMMARY_REASON_KEYS if key in reason})
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


def add_market_scan_summary_to_manifest(manifest: dict) -> dict:
    updated = dict(manifest)
    files = list(updated.get("files") or [])
    if MARKET_SCAN_SUMMARY_FILE not in files:
        if "market_scan_latest.json" in files:
            files.insert(files.index("market_scan_latest.json") + 1, MARKET_SCAN_SUMMARY_FILE)
        else:
            files.append(MARKET_SCAN_SUMMARY_FILE)
    updated["files"] = files
    return updated


def write_market_scan_summary(scan_payload: dict) -> None:
    write_json(OUT_DIR / MARKET_SCAN_SUMMARY_FILE, compact_market_scan_payload(scan_payload))



def rebuild_scan_from_analysis(scan_payload: dict, results: list[dict]) -> dict:
    rebuilt = dict(scan_payload)
    rebuilt["entry"] = [item for item in results if item.get("status") == "ENTRY"]
    rebuilt["excluded"] = [item for item in results if item.get("status") == "EXCLUDED"]
    rebuilt["watch"] = [
        item
        for item in results
        if item.get("status") not in {"ENTRY", "EXCLUDED"}
    ]
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


def history_seed_snapshots(settings) -> list[FundamentalSnapshot]:
    # Build company profile lookup to enrich history records with proper industry/name data
    company_lookup: dict[str, Company] = {c.stockCode: c for c in official_provider.list_companies()}

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
            market = cast(Literal["TWSE", "TPEX", "OTHER"], record.get("market")) if record.get("market") in {"TWSE", "TPEX"} else "OTHER"
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
    return {
        "universeSize": scan_payload.get("universeSize") or 0,
        "companies": len(companies),
        "analysis": len(analysis_by_code),
        "entry": len(scan_payload.get("entry", [])),
        "watch": len(scan_payload.get("watch", [])),
        "excluded": len(scan_payload.get("excluded", [])),
        "fallbackSource": fallback_source,
        "providerStatus": official_provider.status(refresh=False),
    }


def assert_seed_quality(scan_payload: dict, companies: list, analysis_by_code: dict, fallback_source: str | None) -> None:
    diagnostics = seed_diagnostics(scan_payload, companies, analysis_by_code, fallback_source)
    if diagnostics["companies"] < MIN_SEED_COMPANY_SIZE:
        raise RuntimeError(
            "Refusing to publish an undersized company seed: "
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


def copy_offline_seed_payload(path: Path = SEED_CACHE_ZIP) -> dict:
    clear_seed_output()
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        missing = sorted(f"{OFFLINE_SEED_PREFIX}{name}" for name in OFFLINE_SEED_REQUIRED_FILES if f"{OFFLINE_SEED_PREFIX}{name}" not in names)
        if missing:
            raise RuntimeError(f"Offline seed cache is missing required entries: {', '.join(missing)}")

        for name in names:
            if not name.startswith(OFFLINE_SEED_PREFIX) or name.endswith("/"):
                continue
            relative = Path(name.removeprefix(OFFLINE_SEED_PREFIX))
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(f"Unsafe offline seed entry path: {name}")
            target = OUT_DIR / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))

    manifest = json.loads((OUT_DIR / "manifest.json").read_text(encoding="utf-8"))
    scan_payload = json.loads((OUT_DIR / "market_scan_latest.json").read_text(encoding="utf-8"))
    write_market_scan_summary(scan_payload)
    manifest = add_market_scan_summary_to_manifest(manifest)
    counts = manifest.get("counts", {})
    assert_seed_quality(
        {
            "universeSize": sum(int(counts.get(key) or 0) for key in ("entry", "watch", "excluded")),
            "entry": [None] * int(counts.get("entry") or 0),
            "watch": [None] * int(counts.get("watch") or 0),
            "excluded": [None] * int(counts.get("excluded") or 0),
        },
        [None] * int(counts.get("companies") or 0),
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
    next_refresh = generated_at + timedelta(seconds=policy["minIntervalSeconds"])
    companies = official_provider.list_companies()
    analysis_by_code: dict = {}
    analysis_shards: dict[str, dict[str, dict]] = {}
    holding_analysis_by_code: dict = {}
    holding_analysis_shards: dict[str, dict[str, dict]] = {}
    analysis_results: list = []
    fallback_source = None

    _build_analysis_for_snapshots(
        official_provider.list_snapshots(settings),
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
    write_market_scan_summary(scan_payload)
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
        "latestFinancialPeriod": scan_payload.get("filingContext", {}).get("activeFinancialReport", {}).get("period"),
        "cachePolicy": policy,
        "files": [
            "market_scan_latest.json",
            MARKET_SCAN_SUMMARY_FILE,
            "companies.json",
            "analysis_by_code.json",
            "analysis_shards/*.json",
            "holding_analysis_by_code.json",
            "holding_analysis_shards/*.json",
            "data_sources_status.json",
            "official_fundamentals_history.json",
            "official_history_backfill_progress.json",
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
            "minimumUniverseSize": MIN_SEED_UNIVERSE_SIZE,
            "minimumAnalysisSize": MIN_SEED_ANALYSIS_SIZE,
            "fallbackSource": fallback_source,
        },
    }
    write_json(OUT_DIR / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

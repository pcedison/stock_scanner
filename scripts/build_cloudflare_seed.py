from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder

ROOT_DIR = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT_DIR / "cloudflare" / "seed"
ANALYSIS_SHARD_DIR = OUT_DIR / "analysis_shards"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
sys.path.insert(0, str(ROOT_DIR))

from backend.main import data_sources_status, engine, official_provider, _scan_market_payload  # noqa: E402
from backend.models.company import Company  # noqa: E402
from backend.models.financial import FundamentalSnapshot  # noqa: E402
from backend.services.official_data_provider import _is_financial_company  # noqa: E402
from backend.services.settings_service import load_settings  # noqa: E402


MIN_SEED_UNIVERSE_SIZE = int(os.getenv("MIN_SEED_UNIVERSE_SIZE", "1000"))
MIN_SEED_ANALYSIS_SIZE = int(os.getenv("MIN_SEED_ANALYSIS_SIZE", "1000"))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jsonable_encoder(payload), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def clear_generated_analysis_shards() -> None:
    if not ANALYSIS_SHARD_DIR.exists():
        return
    for path in ANALYSIS_SHARD_DIR.glob("*.json"):
        path.unlink()


def analysis_shard_key(stock_code: str) -> str:
    return str(stock_code)[:2]


def refresh_policy(now: datetime | None = None) -> dict:
    current = now or datetime.now(TAIPEI_TZ)
    financial_deadlines = {(3, 31), (5, 15), (5, 30), (8, 31), (11, 14)}
    in_financial_window = any(
        month == current.month and abs((current.date() - current.replace(month=month, day=day).date()).days) <= 3
        for month, day in financial_deadlines
    )
    if in_financial_window:
        return {"strategy": "stale_while_revalidate", "reason": "financial_report_window", "minIntervalSeconds": 7200}
    if 8 <= current.day <= 12:
        return {"strategy": "stale_while_revalidate", "reason": "monthly_revenue_window", "minIntervalSeconds": 10800}
    return {"strategy": "stale_while_revalidate", "reason": "routine_refresh", "minIntervalSeconds": 43200}


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


def period_key(period: str | None) -> tuple[int, int]:
    try:
        year, quarter = str(period or "").upper().split("Q", 1)
        return int(year), int(quarter)
    except (AttributeError, ValueError):
        return 0, 0


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
    if previous_value in (None, 0):
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
    return list({row["year"]: row for row in sorted(annuals, key=lambda item: item["year"])}.values())


def inventory_turnover_from_history(record: dict) -> float | None:
    cost = record.get("costOfRevenue")
    inventory = record.get("inventory")
    quarter = record.get("quarter")
    if cost is None or inventory in (None, 0) or not quarter:
        return None
    return (float(cost) * (4 / int(quarter))) / float(inventory)


def history_seed_snapshots(settings) -> list[FundamentalSnapshot]:
    payload = official_provider.history_store.load()
    quarters = payload.get("quarters", {})
    if not isinstance(quarters, dict):
        return []

    snapshots: list[FundamentalSnapshot] = []
    for stock_code, records in quarters.items():
        if not isinstance(stock_code, str) or not stock_code.isdigit() or not isinstance(records, dict) or not records:
            continue
        latest_period = max(records, key=period_key)
        record = records.get(latest_period)
        if not isinstance(record, dict):
            continue
        market = record.get("market") if record.get("market") in {"TWSE", "TPEX"} else "OTHER"
        if market == "TWSE" and not settings.scan_twse:
            continue
        if market == "TPEX" and not settings.scan_tpex:
            continue
        company_name = str(record.get("companyName") or stock_code)
        industry_name = "Unknown industry"
        quarter_month = max(1, min(12, int(record.get("quarter") or 1) * 3))
        fiscal_year = int(record.get("fiscalYear") or period_key(latest_period)[0] or 1970)
        company = Company(
            stockCode=stock_code,
            name=company_name,
            market=market,
            industryName=industry_name,
            isFinancial=_is_financial_company(stock_code, industry_name, company_name),
        )
        snapshots.append(
            FundamentalSnapshot.model_validate(
                {
                    "company": company.model_dump(),
                    "monthlyRevenue": {
                        "month": f"{fiscal_year}-{quarter_month:02d}",
                        "monthlyRevenueYoY": None,
                        "previousMonthRevenueYoY": None,
                        "cumulativeRevenueYoY": None,
                        "trailingThreeMonthAverageYoY": None,
                        "janFebCombinedRevenueYoY": None,
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


def main() -> None:
    settings = load_settings()
    settings.use_mock_data = False
    settings.manual_scan_enabled = True

    clear_generated_analysis_shards()
    scan_payload = _scan_market_payload(settings)
    policy = refresh_policy()
    generated_at = datetime.fromisoformat(scan_payload["generatedAt"])
    next_refresh = generated_at + timedelta(seconds=policy["minIntervalSeconds"])
    companies = official_provider.list_companies()
    analysis_by_code = {}
    analysis_shards: dict[str, dict[str, dict]] = {}
    analysis_results = []
    fallback_source = None
    for snapshot in official_provider.iter_snapshots(settings):
        result = engine.evaluate_entry(snapshot, settings)
        encoded = jsonable_encoder(result)
        analysis_by_code[result.stockCode] = encoded
        analysis_shards.setdefault(analysis_shard_key(result.stockCode), {})[result.stockCode] = encoded
        analysis_results.append(encoded)

    if not analysis_results:
        history_snapshots = history_seed_snapshots(settings)
        if history_snapshots:
            fallback_source = "official_fundamentals_history"
            companies = [snapshot.company for snapshot in history_snapshots]
            for snapshot in history_snapshots:
                result = engine.evaluate_entry(snapshot, settings)
                encoded = jsonable_encoder(result)
                analysis_by_code[result.stockCode] = encoded
                analysis_shards.setdefault(analysis_shard_key(result.stockCode), {})[result.stockCode] = encoded
                analysis_results.append(encoded)

    if not scan_payload.get("universeSize") and analysis_results:
        scan_payload = rebuild_scan_from_analysis(scan_payload, analysis_results)

    assert_seed_quality(scan_payload, companies, analysis_by_code, fallback_source)

    write_json(OUT_DIR / "market_scan_latest.json", scan_payload)
    write_json(OUT_DIR / "companies.json", {"items": companies})
    write_json(OUT_DIR / "analysis_by_code.json", analysis_by_code)
    for shard_key, shard_payload in analysis_shards.items():
        write_json(ANALYSIS_SHARD_DIR / f"{shard_key}.json", shard_payload)
    write_json(OUT_DIR / "data_sources_status.json", data_sources_status(check_network=False))

    manifest = {
        "generatedAt": scan_payload.get("generatedAt"),
        "sourceLastCheckedAt": scan_payload.get("generatedAt"),
        "nextRefreshAfter": next_refresh.isoformat(),
        "latestRevenuePeriod": scan_payload.get("filingContext", {}).get("monthlyRevenuePeriod"),
        "latestFinancialPeriod": scan_payload.get("filingContext", {}).get("activeFinancialReport", {}).get("period"),
        "cachePolicy": policy,
        "files": [
            "market_scan_latest.json",
            "companies.json",
            "analysis_by_code.json",
            "analysis_shards/*.json",
            "data_sources_status.json",
            "official_fundamentals_history.json",
            "official_history_backfill_progress.json",
            "official_cache_seed_2026-05-14.zip",
        ],
        "counts": {
            "companies": len(companies),
            "entry": len(scan_payload.get("entry", [])),
            "watch": len(scan_payload.get("watch", [])),
            "excluded": len(scan_payload.get("excluded", [])),
            "analysis": len(analysis_by_code),
            "analysisShards": len(analysis_shards),
        },
        "qualityGates": {
            "minimumUniverseSize": MIN_SEED_UNIVERSE_SIZE,
            "minimumAnalysisSize": MIN_SEED_ANALYSIS_SIZE,
            "fallbackSource": fallback_source,
        },
    }
    write_json(OUT_DIR / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

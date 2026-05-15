from __future__ import annotations

import json
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
from backend.services.settings_service import load_settings  # noqa: E402


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
    for snapshot in official_provider.iter_snapshots(settings):
        result = engine.evaluate_entry(snapshot, settings)
        encoded = jsonable_encoder(result)
        analysis_by_code[result.stockCode] = encoded
        analysis_shards.setdefault(analysis_shard_key(result.stockCode), {})[result.stockCode] = encoded
        analysis_results.append(encoded)

    if not scan_payload.get("universeSize") and analysis_results:
        scan_payload = rebuild_scan_from_analysis(scan_payload, analysis_results)

    if not scan_payload.get("universeSize"):
        raise RuntimeError("Refusing to publish an empty market scan seed.")

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
    }
    write_json(OUT_DIR / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

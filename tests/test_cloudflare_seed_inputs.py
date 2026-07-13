from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.validate_cloudflare_seed_inputs import (
    _load_json_from_zip,
    failed_company_summary,
    main,
    render_seed_summary,
    validate_seed_freshness,
    validate_seed_zip,
)

TEST_SEED_COMPANIES = 1700


def _company_items(count: int = TEST_SEED_COMPANIES) -> list[dict[str, str]]:
    return [
        {
            "stockCode": f"{100000 + index:06d}",
            "name": "TWSE company" if index < 1000 else "TPEX company",
            "market": "TWSE" if index < 1000 else "TPEX",
            "industryName": "Other",
        }
        for index in range(count)
    ]


def _write_seed_zip(
    path: Path,
    companies: int = 1000,
    rows_per_company: int = 5,
    seed_companies: int = TEST_SEED_COMPANIES,
) -> None:
    quarters = {}
    for index in range(companies):
        stock_code = f"{index + 1000:04d}"
        quarters[stock_code] = {
            f"202{i}Q4": {"stockCode": stock_code, "period": f"202{i}Q4"} for i in range(rows_per_company)
        }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("official_fundamentals_history.json", json.dumps({"quarters": quarters}))
        archive.writestr("official_history_backfill_progress.json", "{}")
        archive.writestr(
            "monthly_revenue_history.json",
            json.dumps(
                {
                    "months": {
                        stock_code: {
                            "2026-05": {"monthlyRevenueYoY": 10.0},
                            "2026-06": {"monthlyRevenueYoY": 12.0},
                        }
                        for stock_code in quarters
                    }
                }
            ),
        )
        archive.writestr(
            "cloudflare_seed/manifest.json",
            json.dumps(
                {
                    "generatedAt": "2026-05-17T00:00:00+00:00",
                    "latestRevenuePeriod": "2026-04",
                    "latestFinancialPeriod": "2026Q1",
                    "counts": {
                        "companies": seed_companies,
                        "entry": 10,
                        "watch": companies - 20,
                        "excluded": 10,
                        "analysis": companies,
                        "analysisShards": 1,
                        "holdingAnalysis": companies,
                        "holdingAnalysisShards": 1,
                    },
                }
            ),
        )
        archive.writestr("cloudflare_seed/companies.json", json.dumps({"items": _company_items(seed_companies)}))
        archive.writestr("cloudflare_seed/data_sources_status.json", "{}")
        archive.writestr(
            "cloudflare_seed/market_scan_latest.json",
            json.dumps(
                {
                    "universeSize": companies,
                    "entry": [{} for _ in range(10)],
                    "watch": [{} for _ in range(companies - 20)],
                    "excluded": [{} for _ in range(10)],
                }
            ),
        )
        archive.writestr("cloudflare_seed/analysis_by_code.json", "{}")
        archive.writestr("cloudflare_seed/holding_analysis_by_code.json", "{}")
        archive.writestr("cloudflare_seed/analysis_shards/10.json", "{}")
        archive.writestr("cloudflare_seed/holding_analysis_shards/10.json", "{}")


def test_validate_seed_zip_accepts_populated_history(tmp_path):
    archive_path = tmp_path / "seed.zip"
    _write_seed_zip(archive_path)

    summary = validate_seed_zip(archive_path)

    assert summary["companies"] == 1000
    assert summary["quarterlyRows"] == 5000
    assert summary["latestPeriod"] == "2024Q4"
    assert summary["seedCompanies"] == TEST_SEED_COMPANIES
    assert summary["seedAnalysis"] == 1000
    assert summary["seedHoldingAnalysis"] == 1000
    assert summary["seedShards"] == 1
    assert summary["seedHoldingShards"] == 1
    assert summary["generatedAt"] == "2026-05-17T00:00:00+00:00"
    assert summary["latestRevenueHistoryMonth"] == "2026-06"
    assert summary["consecutiveRevenueHistoryCompanies"] == 1000


def test_seed_freshness_rejects_stale_manifest():
    summary = {"generatedAt": "2026-01-01T00:00:00+00:00"}

    with pytest.raises(ValueError, match="maximum allowed"):
        validate_seed_freshness(summary, max_age_days=45, now=datetime(2026, 5, 18, tzinfo=UTC))


def test_validate_seed_zip_rejects_empty_history(tmp_path):
    archive_path = tmp_path / "seed.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("official_fundamentals_history.json", json.dumps({"quarters": {}}))
        archive.writestr("official_history_backfill_progress.json", "{}")
        archive.writestr(
            "monthly_revenue_history.json",
            json.dumps(
                {
                    "months": {
                        f"{index + 1000:04d}": {
                            "2026-05": {"monthlyRevenueYoY": 10.0},
                            "2026-06": {"monthlyRevenueYoY": 12.0},
                        }
                        for index in range(1000)
                    }
                }
            ),
        )
        archive.writestr("cloudflare_seed/manifest.json", json.dumps({"counts": {}}))
        archive.writestr("cloudflare_seed/companies.json", "{}")
        archive.writestr("cloudflare_seed/data_sources_status.json", "{}")
        archive.writestr("cloudflare_seed/market_scan_latest.json", "{}")
        archive.writestr("cloudflare_seed/analysis_by_code.json", "{}")
        archive.writestr("cloudflare_seed/holding_analysis_by_code.json", "{}")
        archive.writestr("cloudflare_seed/analysis_shards/10.json", "{}")
        archive.writestr("cloudflare_seed/holding_analysis_shards/10.json", "{}")

    with pytest.raises(ValueError, match="companies"):
        validate_seed_zip(archive_path)


def test_validate_seed_zip_rejects_missing_required_entry(tmp_path):
    archive_path = tmp_path / "seed.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("official_fundamentals_history.json", json.dumps({"quarters": {}}))

    with pytest.raises(ValueError, match="missing required entries"):
        validate_seed_zip(archive_path)


def test_validate_seed_zip_requires_consecutive_monthly_history(tmp_path):
    members = _seed_members()
    members["monthly_revenue_history.json"] = json.dumps(
        {
            "months": {
                f"{index + 1000:04d}": {
                    "2026-04": {"monthlyRevenueYoY": 10.0},
                    "2026-06": {"monthlyRevenueYoY": 12.0},
                }
                for index in range(1000)
            }
        }
    )
    archive_path = _zip_from(tmp_path, members)

    with pytest.raises(ValueError, match="consecutive monthly revenue history"):
        validate_seed_zip(archive_path)


def test_seed_quality_summary_includes_failed_reasons(tmp_path):
    failed_csv = tmp_path / "failed.csv"
    failed_csv.write_text(
        "stock_code,initial_reason,manual_status\n1234,missing 2026Q1,todo\n5678,missing 2026Q1,patched\n",
        encoding="utf-8",
    )
    summary = {
        "zip": "seed.zip",
        "companies": 1000,
        "quarterlyRows": 5000,
        "latestPeriod": "2026Q1",
        "seedCompanies": 1000,
        "seedAnalysis": 1000,
        "seedHoldingAnalysis": 1000,
        "seedUniverse": 1000,
        "seedShards": 10,
        "seedHoldingShards": 10,
    }

    failed = failed_company_summary(failed_csv)
    markdown = render_seed_summary(summary, failed)

    assert failed["failedCompanies"] == 2
    assert failed["manualStatus"] == {"patched": 1, "todo": 1}
    assert "missing 2026Q1: 2" in markdown


def _seed_members(
    companies: int = 1000,
    rows: int = 5,
    manifest: object | None = None,
    seed_company_count: int = TEST_SEED_COMPANIES,
) -> dict[str, str]:
    quarters = {
        f"{index + 1000:04d}": {f"202{i}Q4": {"period": f"202{i}Q4"} for i in range(rows)} for index in range(companies)
    }
    default_manifest = {
        "generatedAt": "2026-05-17T00:00:00+00:00",
        "counts": {
            "companies": TEST_SEED_COMPANIES,
            "entry": 10,
            "watch": companies - 20,
            "excluded": 10,
            "analysis": companies,
            "holdingAnalysis": companies,
        },
    }
    selected_manifest = default_manifest if manifest is None else manifest
    scan_counts = {"entry": 10, "watch": companies - 20, "excluded": 10}
    if isinstance(selected_manifest, dict) and isinstance(selected_manifest.get("counts"), dict):
        candidate_counts = {
            category: selected_manifest["counts"].get(category) for category in ("entry", "watch", "excluded")
        }
        if all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in candidate_counts.values()
        ):
            scan_counts = candidate_counts
    return {
        "official_fundamentals_history.json": json.dumps({"quarters": quarters}),
        "official_history_backfill_progress.json": "{}",
        "monthly_revenue_history.json": json.dumps(
            {
                "months": {
                    stock_code: {
                        "2026-05": {"monthlyRevenueYoY": 10.0},
                        "2026-06": {"monthlyRevenueYoY": 12.0},
                    }
                    for stock_code in quarters
                }
            }
        ),
        "cloudflare_seed/manifest.json": json.dumps(selected_manifest),
        "cloudflare_seed/companies.json": json.dumps({"items": _company_items(seed_company_count)}),
        "cloudflare_seed/data_sources_status.json": "{}",
        "cloudflare_seed/market_scan_latest.json": json.dumps(
            {
                "universeSize": sum(scan_counts.values()),
                "entry": [{} for _ in range(scan_counts["entry"])],
                "watch": [{} for _ in range(scan_counts["watch"])],
                "excluded": [{} for _ in range(scan_counts["excluded"])],
            }
        ),
        "cloudflare_seed/analysis_by_code.json": "{}",
        "cloudflare_seed/holding_analysis_by_code.json": "{}",
        "cloudflare_seed/analysis_shards/10.json": "{}",
        "cloudflare_seed/holding_analysis_shards/10.json": "{}",
    }


def _zip_from(tmp_path: Path, members: dict[str, str], name: str = "seed.zip") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        for entry, content in members.items():
            archive.writestr(entry, content)
    return path


def test_validate_seed_zip_requires_existing_file(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        validate_seed_zip(tmp_path / "absent.zip")


def test_validate_seed_zip_requires_shard_entries(tmp_path):
    members = _seed_members()
    del members["cloudflare_seed/analysis_shards/10.json"]
    with pytest.raises(ValueError, match="analysis_shards"):
        validate_seed_zip(_zip_from(tmp_path, members))

    members = _seed_members()
    del members["cloudflare_seed/holding_analysis_shards/10.json"]
    with pytest.raises(ValueError, match="holding_analysis_shards"):
        validate_seed_zip(_zip_from(tmp_path, members, name="seed2.zip"))


def test_validate_seed_zip_rejects_corrupt_and_malformed_payloads(tmp_path):
    members = _seed_members()
    members["official_fundamentals_history.json"] = "{not json"
    with pytest.raises(ValueError, match="not valid JSON"):
        validate_seed_zip(_zip_from(tmp_path, members))

    members = _seed_members()
    members["official_fundamentals_history.json"] = "[]"
    with pytest.raises(ValueError, match="must be a JSON object"):
        validate_seed_zip(_zip_from(tmp_path, members, name="a.zip"))

    members = _seed_members()
    members["official_fundamentals_history.json"] = json.dumps({"quarters": []})
    with pytest.raises(ValueError, match="quarters object"):
        validate_seed_zip(_zip_from(tmp_path, members, name="b.zip"))

    members = _seed_members(rows=1)
    with pytest.raises(ValueError, match="quarterly rows"):
        validate_seed_zip(_zip_from(tmp_path, members, name="c.zip"))

    members = _seed_members(manifest=[])
    with pytest.raises(ValueError, match="manifest.json must be a JSON object"):
        validate_seed_zip(_zip_from(tmp_path, members, name="d.zip"))

    members = _seed_members(manifest={"counts": []})
    with pytest.raises(ValueError, match="counts object"):
        validate_seed_zip(_zip_from(tmp_path, members, name="e.zip"))


@pytest.mark.parametrize(
    ("counts", "match"),
    [
        (
            {"companies": 5, "analysis": 1000, "holdingAnalysis": 1000, "entry": 400, "watch": 400, "excluded": 400},
            "companies",
        ),
        (
            {"companies": 1700, "analysis": 5, "holdingAnalysis": 1000, "entry": 400, "watch": 400, "excluded": 400},
            "analysis rows",
        ),
        (
            {"companies": 1700, "analysis": 1000, "holdingAnalysis": 5, "entry": 400, "watch": 400, "excluded": 400},
            "holding analysis",
        ),
        (
            {"companies": 1700, "analysis": 1000, "holdingAnalysis": 1000, "entry": 1, "watch": 1, "excluded": 1},
            "universe",
        ),
    ],
)
def test_validate_seed_zip_enforces_seed_count_thresholds(tmp_path, counts, match):
    manifest = {"generatedAt": "2026-05-17T00:00:00+00:00", "counts": counts}
    members = _seed_members(manifest=manifest, seed_company_count=counts["companies"])
    with pytest.raises(ValueError, match=match):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_freshness_edge_cases():
    # max_age_days=None disables the check entirely (no exception even when ancient).
    validate_seed_freshness({"generatedAt": "2026-01-01T00:00:00+00:00"}, max_age_days=None)

    with pytest.raises(ValueError, match="missing generatedAt"):
        validate_seed_freshness({}, max_age_days=30)

    with pytest.raises(ValueError, match="not a valid ISO"):
        validate_seed_freshness({"generatedAt": "yesterday"}, max_age_days=30)

    # Naive timestamp is treated as UTC and stays within budget (no raise).
    validate_seed_freshness(
        {"generatedAt": "2026-05-17T00:00:00"},
        max_age_days=30,
        now=datetime(2026, 5, 18, tzinfo=UTC),
    )


def test_failed_company_summary_missing_file_and_subagent_fallback(tmp_path):
    summary = failed_company_summary(tmp_path / "none.csv")
    assert summary["failedCompanies"] == 0
    assert summary["manualStatus"] == {}

    csv_path = tmp_path / "failed.csv"
    csv_path.write_text(
        "stock_code,initial_reason,subagent_status\n1234,missing 2026Q1,in_progress\n",
        encoding="utf-8",
    )
    summary = failed_company_summary(csv_path)
    assert summary["manualStatus"] == {"in_progress": 1}


def test_render_seed_summary_handles_no_manual_followup():
    summary = {
        "zip": "seed.zip",
        "companies": 1000,
        "quarterlyRows": 5000,
        "latestPeriod": "2026Q1",
        "seedCompanies": 1000,
        "seedAnalysis": 1000,
        "seedHoldingAnalysis": 1000,
        "seedUniverse": 1000,
        "seedShards": 10,
        "seedHoldingShards": 10,
    }
    markdown = render_seed_summary(summary, {"failedCompanies": 0, "manualStatus": {}, "reasons": {}})
    assert "- none" in markdown


def test_main_writes_summary_outputs_and_reports_failure(tmp_path, capsys):
    valid = _zip_from(tmp_path, _seed_members())
    md = tmp_path / "out" / "summary.md"
    js = tmp_path / "out" / "summary.json"
    missing_csv = tmp_path / "none.csv"

    rc = main(
        [
            "--zip",
            str(valid),
            "--summary-md",
            str(md),
            "--summary-json",
            str(js),
            "--failed-companies-csv",
            str(missing_csv),
        ]
    )
    assert rc == 0
    assert md.exists() and "Cloudflare seed quality" in md.read_text(encoding="utf-8")
    assert json.loads(js.read_text(encoding="utf-8"))["seed"]["seedCompanies"] == TEST_SEED_COMPANIES

    empty = _zip_from(tmp_path, {"official_fundamentals_history.json": "{}"}, name="empty.zip")
    assert main(["--zip", str(empty)]) == 1


def test_load_json_from_zip_reports_missing_entry(tmp_path):
    path = _zip_from(tmp_path, {"present.json": "{}"})
    with zipfile.ZipFile(path) as archive:
        with pytest.raises(ValueError, match="Missing required seed entry"):
            _load_json_from_zip(archive, "absent.json")


def test_validate_seed_zip_skips_malformed_quarter_entries(tmp_path):
    members = _seed_members()
    history = json.loads(members["official_fundamentals_history.json"])
    history["quarters"]["BADCODE"] = "not-a-records-dict"
    history["quarters"]["9999"] = {}
    members["official_fundamentals_history.json"] = json.dumps(history)

    summary = validate_seed_zip(_zip_from(tmp_path, members))
    # The two malformed entries are skipped; the 1000 valid companies still validate.
    assert summary["companies"] == 1000


@pytest.mark.parametrize("category", ["entry", "watch", "excluded"])
def test_validate_seed_zip_requires_market_scan_category_lists(tmp_path, category):
    members = _seed_members()
    scan = json.loads(members["cloudflare_seed/market_scan_latest.json"])
    scan[category] = {}
    members["cloudflare_seed/market_scan_latest.json"] = json.dumps(scan)

    with pytest.raises(ValueError, match=rf"market_scan_latest\.json {category} must be a JSON list"):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_zip_rejects_empty_market_scan(tmp_path):
    members = _seed_members()
    members["cloudflare_seed/market_scan_latest.json"] = json.dumps(
        {"universeSize": 0, "entry": [], "watch": [], "excluded": []}
    )

    with pytest.raises(ValueError, match="market scan has no rows"):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_zip_rejects_market_scan_universe_mismatch(tmp_path):
    members = _seed_members()
    scan = json.loads(members["cloudflare_seed/market_scan_latest.json"])
    scan["universeSize"] = 999
    members["cloudflare_seed/market_scan_latest.json"] = json.dumps(scan)

    with pytest.raises(ValueError, match="market scan universeSize 999 does not match actual total 1000"):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_zip_rejects_manifest_category_count_mismatch(tmp_path):
    members = _seed_members()
    manifest = json.loads(members["cloudflare_seed/manifest.json"])
    manifest["counts"]["watch"] = 981
    members["cloudflare_seed/manifest.json"] = json.dumps(manifest)

    with pytest.raises(ValueError, match="manifest counts.watch 981 does not match market scan 980"):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_zip_rejects_optional_manifest_scan_totals_mismatch(tmp_path):
    members = _seed_members()
    manifest = json.loads(members["cloudflare_seed/manifest.json"])
    manifest["categoryCounts"] = {"entry": 10, "watch": 979, "excluded": 10}
    manifest["universeSize"] = 999
    members["cloudflare_seed/manifest.json"] = json.dumps(manifest)

    with pytest.raises(ValueError, match="manifest categoryCounts.watch 979 does not match market scan 980"):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_zip_rejects_optional_manifest_universe_mismatch(tmp_path):
    members = _seed_members()
    manifest = json.loads(members["cloudflare_seed/manifest.json"])
    manifest["universeSize"] = 999
    members["cloudflare_seed/manifest.json"] = json.dumps(manifest)

    with pytest.raises(ValueError, match="manifest universeSize 999 does not match market scan 1000"):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_zip_rejects_manifest_company_count_mismatch_with_items(tmp_path):
    members = _seed_members()
    manifest = json.loads(members["cloudflare_seed/manifest.json"])
    manifest["counts"]["companies"] = 1699
    members["cloudflare_seed/manifest.json"] = json.dumps(manifest)

    with pytest.raises(ValueError, match="counts.companies 1699 does not match companies.json items 1700"):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_zip_rejects_single_market_even_when_total_is_large(tmp_path):
    members = _seed_members()
    companies = json.loads(members["cloudflare_seed/companies.json"])
    for item in companies["items"]:
        item["market"] = "TWSE"
    members["cloudflare_seed/companies.json"] = json.dumps(companies)
    manifest = json.loads(members["cloudflare_seed/manifest.json"])
    manifest["counts"]["companies"] = 1700
    members["cloudflare_seed/manifest.json"] = json.dumps(manifest)

    with pytest.raises(ValueError, match="TPEX company coverage 0 is below required minimum 700"):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_zip_rejects_manifest_company_market_count_mismatch(tmp_path):
    members = _seed_members()
    manifest = json.loads(members["cloudflare_seed/manifest.json"])
    manifest["counts"]["companies"] = 1700
    manifest["companiesByMarket"] = {"TWSE": 999, "TPEX": 701}
    members["cloudflare_seed/manifest.json"] = json.dumps(manifest)

    with pytest.raises(ValueError, match="companiesByMarket does not match companies.json"):
        validate_seed_zip(_zip_from(tmp_path, members))


@pytest.mark.parametrize(
    "private_path",
    [
        r"C:\Users\someone\stock-scanner\data\cache.json",
        "/Users/someone/stock-scanner/data/cache.json",
        "/home/someone/stock-scanner/data/cache.json",
    ],
)
def test_validate_seed_zip_rejects_nested_local_user_paths(tmp_path, private_path):
    members = _seed_members()
    members["cloudflare_seed/data_sources_status.json"] = json.dumps(
        {"official": {"history": [{"source": {"path": private_path}}]}}
    )

    with pytest.raises(ValueError, match="contains a local user path"):
        validate_seed_zip(_zip_from(tmp_path, members))

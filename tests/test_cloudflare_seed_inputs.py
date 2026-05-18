from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.validate_cloudflare_seed_inputs import (
    failed_company_summary,
    render_seed_summary,
    validate_seed_freshness,
    validate_seed_zip,
)


def _write_seed_zip(path: Path, companies: int = 1000, rows_per_company: int = 5) -> None:
    quarters = {}
    for index in range(companies):
        stock_code = f"{index + 1000:04d}"
        quarters[stock_code] = {
            f"202{i}Q4": {"stockCode": stock_code, "period": f"202{i}Q4"}
            for i in range(rows_per_company)
        }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("official_fundamentals_history.json", json.dumps({"quarters": quarters}))
        archive.writestr("official_history_backfill_progress.json", "{}")
        archive.writestr(
            "cloudflare_seed/manifest.json",
            json.dumps(
                {
                    "generatedAt": "2026-05-17T00:00:00+00:00",
                    "latestRevenuePeriod": "2026-04",
                    "latestFinancialPeriod": "2026Q1",
                    "counts": {
                        "companies": companies,
                        "entry": 10,
                        "watch": companies - 20,
                        "excluded": 10,
                        "analysis": companies,
                        "analysisShards": 1,
                    }
                }
            ),
        )
        archive.writestr("cloudflare_seed/companies.json", json.dumps({"items": []}))
        archive.writestr("cloudflare_seed/data_sources_status.json", "{}")
        archive.writestr("cloudflare_seed/market_scan_latest.json", "{}")
        archive.writestr("cloudflare_seed/analysis_by_code.json", "{}")
        archive.writestr("cloudflare_seed/analysis_shards/10.json", "{}")


def test_validate_seed_zip_accepts_populated_history(tmp_path):
    archive_path = tmp_path / "seed.zip"
    _write_seed_zip(archive_path)

    summary = validate_seed_zip(archive_path)

    assert summary["companies"] == 1000
    assert summary["quarterlyRows"] == 5000
    assert summary["latestPeriod"] == "2024Q4"
    assert summary["seedCompanies"] == 1000
    assert summary["seedAnalysis"] == 1000
    assert summary["seedShards"] == 1
    assert summary["generatedAt"] == "2026-05-17T00:00:00+00:00"


def test_seed_freshness_rejects_stale_manifest():
    summary = {"generatedAt": "2026-01-01T00:00:00+00:00"}

    with pytest.raises(ValueError, match="maximum allowed"):
        validate_seed_freshness(summary, max_age_days=45, now=datetime(2026, 5, 18, tzinfo=timezone.utc))


def test_validate_seed_zip_rejects_empty_history(tmp_path):
    archive_path = tmp_path / "seed.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("official_fundamentals_history.json", json.dumps({"quarters": {}}))
        archive.writestr("official_history_backfill_progress.json", "{}")
        archive.writestr("cloudflare_seed/manifest.json", json.dumps({"counts": {}}))
        archive.writestr("cloudflare_seed/companies.json", "{}")
        archive.writestr("cloudflare_seed/data_sources_status.json", "{}")
        archive.writestr("cloudflare_seed/market_scan_latest.json", "{}")
        archive.writestr("cloudflare_seed/analysis_by_code.json", "{}")
        archive.writestr("cloudflare_seed/analysis_shards/10.json", "{}")

    with pytest.raises(ValueError, match="companies"):
        validate_seed_zip(archive_path)


def test_validate_seed_zip_rejects_missing_required_entry(tmp_path):
    archive_path = tmp_path / "seed.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("official_fundamentals_history.json", json.dumps({"quarters": {}}))

    with pytest.raises(ValueError, match="missing required entries"):
        validate_seed_zip(archive_path)


def test_seed_quality_summary_includes_failed_reasons(tmp_path):
    failed_csv = tmp_path / "failed.csv"
    failed_csv.write_text(
        "stock_code,initial_reason,manual_status\n"
        "1234,missing 2026Q1,todo\n"
        "5678,missing 2026Q1,patched\n",
        encoding="utf-8",
    )
    summary = {
        "zip": "seed.zip",
        "companies": 1000,
        "quarterlyRows": 5000,
        "latestPeriod": "2026Q1",
        "seedCompanies": 1000,
        "seedAnalysis": 1000,
        "seedUniverse": 1000,
        "seedShards": 10,
    }

    failed = failed_company_summary(failed_csv)
    markdown = render_seed_summary(summary, failed)

    assert failed["failedCompanies"] == 2
    assert failed["manualStatus"] == {"patched": 1, "todo": 1}
    assert "missing 2026Q1: 2" in markdown

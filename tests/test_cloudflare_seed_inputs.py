from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts.validate_cloudflare_seed_inputs import validate_seed_zip


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

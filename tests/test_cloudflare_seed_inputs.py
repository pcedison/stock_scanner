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
                        "holdingAnalysis": companies,
                        "holdingAnalysisShards": 1,
                    }
                }
            ),
        )
        archive.writestr("cloudflare_seed/companies.json", json.dumps({"items": []}))
        archive.writestr("cloudflare_seed/data_sources_status.json", "{}")
        archive.writestr("cloudflare_seed/market_scan_latest.json", "{}")
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
    assert summary["seedCompanies"] == 1000
    assert summary["seedAnalysis"] == 1000
    assert summary["seedHoldingAnalysis"] == 1000
    assert summary["seedShards"] == 1
    assert summary["seedHoldingShards"] == 1
    assert summary["generatedAt"] == "2026-05-17T00:00:00+00:00"


def test_seed_freshness_rejects_stale_manifest():
    summary = {"generatedAt": "2026-01-01T00:00:00+00:00"}

    with pytest.raises(ValueError, match="maximum allowed"):
        validate_seed_freshness(summary, max_age_days=45, now=datetime(2026, 5, 18, tzinfo=UTC))


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


def _seed_members(companies: int = 1000, rows: int = 5, manifest: object | None = None) -> dict[str, str]:
    quarters = {
        f"{index + 1000:04d}": {f"202{i}Q4": {"period": f"202{i}Q4"} for i in range(rows)}
        for index in range(companies)
    }
    default_manifest = {
        "generatedAt": "2026-05-17T00:00:00+00:00",
        "counts": {
            "companies": companies,
            "entry": 10,
            "watch": companies - 20,
            "excluded": 10,
            "analysis": companies,
            "holdingAnalysis": companies,
        },
    }
    return {
        "official_fundamentals_history.json": json.dumps({"quarters": quarters}),
        "official_history_backfill_progress.json": "{}",
        "cloudflare_seed/manifest.json": json.dumps(default_manifest if manifest is None else manifest),
        "cloudflare_seed/companies.json": "{}",
        "cloudflare_seed/data_sources_status.json": "{}",
        "cloudflare_seed/market_scan_latest.json": "{}",
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
        ({"companies": 5, "analysis": 1000, "holdingAnalysis": 1000, "entry": 400, "watch": 400, "excluded": 400}, "companies"),
        ({"companies": 1000, "analysis": 5, "holdingAnalysis": 1000, "entry": 400, "watch": 400, "excluded": 400}, "analysis rows"),
        ({"companies": 1000, "analysis": 1000, "holdingAnalysis": 5, "entry": 400, "watch": 400, "excluded": 400}, "holding analysis"),
        ({"companies": 1000, "analysis": 1000, "holdingAnalysis": 1000, "entry": 1, "watch": 1, "excluded": 1}, "universe"),
    ],
)
def test_validate_seed_zip_enforces_seed_count_thresholds(tmp_path, counts, match):
    manifest = {"generatedAt": "2026-05-17T00:00:00+00:00", "counts": counts}
    members = _seed_members(manifest=manifest)
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

    rc = main([
        "--zip", str(valid),
        "--summary-md", str(md),
        "--summary-json", str(js),
        "--failed-companies-csv", str(missing_csv),
    ])
    assert rc == 0
    assert md.exists() and "Cloudflare seed quality" in md.read_text(encoding="utf-8")
    assert json.loads(js.read_text(encoding="utf-8"))["seed"]["seedCompanies"] == 1000

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

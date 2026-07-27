import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import scripts.build_cloudflare_seed as seed_build
from backend.adapters.monthly_revenue_history import MonthlyRevenueHistoryStore
from backend.models.analysis import AnalysisResult, RuleResult
from backend.models.company import Company
from backend.models.financial import FundamentalSnapshot


class ObjectResult:
    def __init__(self, **values):
        self.__dict__.update(values)


class FakeProvider:
    def status(self, refresh=False):
        return {"companies": 0, "monthlySnapshots": 1000, "lastError": "profile endpoint returned no companies"}


def _market_companies(twse: int = 1000, tpex: int = 700) -> list[Company]:
    return [
        Company(stockCode=f"{100000 + index:06d}", name="TWSE", market="TWSE", industryName="Other")
        for index in range(twse)
    ] + [
        Company(stockCode=f"{200000 + index:06d}", name="TPEX", market="TPEX", industryName="Other")
        for index in range(tpex)
    ]


def _v2_scan_fixture(_settings=None) -> dict:
    announced_reason = {
        "code": "OFFICIAL_Q",
        "title": "official quarter",
        "passed": True,
        "severity": "INFO",
        "message": "2026Q1 official report",
    }
    return {
        "generatedAt": "2026-07-13T01:02:03+00:00",
        "filingContext": {
            "freshnessFinancialReport": {"period": "2026Q1"},
            "activeFinancialReport": {"period": "2026Q2"},
            "monthlyRevenuePeriod": "2026-06",
        },
        "financialFreshness": {"latestCachedFinancialPeriod": "2026Q1"},
        "universeSize": 1,
        "entry": [
            {
                "stockCode": "1234",
                "companyName": "Demo",
                "status": "ENTRY",
                "summary": "summary",
                "reasons": [announced_reason],
            }
        ],
        "watch": [],
        "excluded": [],
        "results": [],
    }


def test_merge_seed_companies_unions_snapshot_companies_without_dropping_profiles():
    profile_company = Company(
        stockCode="9999",
        name="Profile name",
        market="TWSE",
        industryName="Other",
    )
    snapshot_duplicate = Company(
        stockCode="9999",
        name="Snapshot name",
        market="TWSE",
        industryName="Other",
    )
    snapshot_only = Company(
        stockCode="1111",
        name="Snapshot only",
        market="TPEX",
        industryName="Technology",
    )

    merged = seed_build.merge_seed_companies(
        [profile_company],
        [
            cast(FundamentalSnapshot, SimpleNamespace(company=snapshot_duplicate)),
            cast(FundamentalSnapshot, SimpleNamespace(company=snapshot_only)),
        ],
    )

    assert [company.stockCode for company in merged] == ["1111", "9999"]
    assert merged[1].name == "Profile name"


def test_compact_market_scan_payload_strips_heavy_evidence():
    payload = {
        "generatedAt": "2026-05-19T00:00:00+00:00",
        "entry": [
            {
                "stockCode": "1234",
                "companyName": "Demo",
                "status": "ENTRY",
                "summary": "summary",
                "largeField": "drop me",
                "reasons": [
                    {
                        "code": "E1",
                        "title": "Rule",
                        "passed": True,
                        "severity": "INFO",
                        "message": "ok",
                        "evidence": [{"label": "heavy"}],
                    },
                    {
                        "code": "E4",
                        "title": "PER",
                        "passed": True,
                        "severity": "INFO",
                        "message": "PER 為 12.3。",
                        "evidence": [{"label": "heavy"}],
                    },
                    {
                        "code": "X2",
                        "title": "Revenue cooldown",
                        "passed": False,
                        "severity": "WARNING",
                        "message": "drop",
                        "evidence": [{"label": "heavy"}],
                    },
                ],
            }
        ],
        "watch": [],
        "excluded": [],
    }

    compact = seed_build.compact_market_scan_payload(payload)

    assert compact["detailMode"] == "summary"
    assert compact["entry"][0]["stockCode"] == "1234"
    assert compact["entry"][0]["detailsAvailable"] is True
    assert "largeField" not in compact["entry"][0]
    assert [reason["code"] for reason in compact["entry"][0]["reasons"]] == ["E4", "X2"]
    assert all("evidence" not in reason for reason in compact["entry"][0]["reasons"])


def test_lazy_market_scan_payload_is_rebuilt_from_analysis_results():
    lazy_scan = {
        "generatedAt": "2026-05-20T00:00:00+00:00",
        "universeSize": 3,
        "entry": [{"detailsAvailable": True, "hasFullDetails": False}],
        "watch": [{"detailsAvailable": True, "hasFullDetails": False}],
        "excluded": [{"detailsAvailable": True, "hasFullDetails": False}],
    }
    analysis_results = [
        {"stockCode": "1234", "companyName": "Entry Co", "status": "ENTRY"},
        {"stockCode": "2345", "companyName": "Watch Co", "status": "INSUFFICIENT_DATA"},
        {"stockCode": "3456", "companyName": "Excluded Co", "status": "EXCLUDED"},
    ]

    assert seed_build.scan_payload_needs_rebuild(lazy_scan) is True
    rebuilt = seed_build.rebuild_scan_from_analysis(lazy_scan, analysis_results)
    compact = seed_build.compact_market_scan_payload(rebuilt)

    assert compact["entry"][0]["stockCode"] == "1234"
    assert compact["watch"][0]["stockCode"] == "2345"
    assert compact["excluded"][0]["stockCode"] == "3456"
    assert compact["universeSize"] == 3


def test_complete_market_scan_payload_does_not_need_rebuild():
    scan = {
        "universeSize": 1,
        "entry": [{"stockCode": "1234", "companyName": "Demo", "status": "ENTRY"}],
        "watch": [],
        "excluded": [],
    }

    assert seed_build.scan_payload_needs_rebuild(scan) is False


def test_object_market_scan_payload_is_compacted_without_losing_identity():
    scan = {
        "universeSize": 1,
        "entry": [
            ObjectResult(
                stockCode="1234",
                companyName="Object Co",
                status="ENTRY",
                summary="summary",
                reasons=[{"code": "OFFICIAL_Q", "severity": "INFO", "message": "2026Q1"}],
            )
        ],
        "watch": [],
        "excluded": [],
    }

    assert seed_build.scan_payload_needs_rebuild(scan) is False
    compact = seed_build.compact_market_scan_payload(scan)

    assert compact["entry"][0]["stockCode"] == "1234"
    assert compact["entry"][0]["companyName"] == "Object Co"
    assert compact["entry"][0]["status"] == "ENTRY"
    assert compact["entry"][0]["reasons"][0]["code"] == "OFFICIAL_Q"


def test_seed_quality_rejects_empty_companies_when_analysis_is_present(monkeypatch):
    monkeypatch.setattr(seed_build, "official_provider", FakeProvider())
    scan_payload = {
        "universeSize": 1000,
        "entry": [],
        "watch": [{} for _ in range(1000)],
        "excluded": [],
    }
    analysis_by_code: dict[str, dict] = {str(index): {} for index in range(1000)}

    with pytest.raises(RuntimeError, match="undersized company seed"):
        seed_build.assert_seed_quality(scan_payload, [], analysis_by_code, fallback_source=None)


def test_seed_quality_rejects_missing_market_company_coverage(monkeypatch):
    monkeypatch.setattr(seed_build, "official_provider", FakeProvider())
    monkeypatch.setattr(seed_build, "MIN_SEED_COMPANY_SIZE", 3)
    monkeypatch.setattr(seed_build, "MIN_SEED_TWSE_COMPANIES", 2, raising=False)
    monkeypatch.setattr(seed_build, "MIN_SEED_TPEX_COMPANIES", 1, raising=False)
    scan_payload = {
        "universeSize": 1000,
        "entry": [],
        "watch": [{} for _ in range(1000)],
        "excluded": [],
    }
    analysis_by_code: dict[str, dict] = {str(index): {} for index in range(1000)}
    companies = [SimpleNamespace(market="TWSE") for _ in range(3)]

    with pytest.raises(RuntimeError, match="TPEX company market coverage"):
        seed_build.assert_seed_quality(scan_payload, companies, analysis_by_code, fallback_source=None)


def test_period_key_parses_quarters_and_tolerates_garbage():
    assert seed_build.period_key("2024Q4") == (2024, 4)
    assert seed_build.period_key("garbage") == (0, 0)
    assert seed_build.period_key(None) == (0, 0)


def test_history_yoy_and_margin_delta():
    records = {
        "2023Q4": {"eps": 2.0, "grossMargin": 40.0},
        "2024Q4": {"fiscalYear": 2024, "quarter": 4, "eps": 3.0, "grossMargin": 45.0},
    }
    current = records["2024Q4"]
    assert seed_build.history_yoy(records, current, "eps") == 50.0
    assert seed_build.history_margin_delta(records, current, "grossMargin") == 5.0
    # Missing previous year, missing fields, and zero baseline all yield None.
    assert seed_build.history_yoy({}, current, "eps") is None
    assert seed_build.history_yoy(records, {"fiscalYear": 2024, "quarter": 4, "eps": None}, "eps") is None
    assert seed_build.history_yoy({"2023Q4": {"eps": 0}}, current, "eps") is None
    assert seed_build.history_margin_delta({"2023Q4": {}}, current, "grossMargin") is None


def test_annual_financials_dedupes_q4_records_by_year():
    records = {
        "2023Q4": {"quarter": 4, "fiscalYear": 2023, "netIncome": 100},
        "2024Q2": {"quarter": 2, "fiscalYear": 2024, "netIncome": 50},
        "2024Q4": {"quarter": 4, "fiscalYear": 2024, "netIncome": 200},
    }
    annuals = seed_build.annual_financials_from_history(records)
    assert [row["year"] for row in annuals] == [2023, 2024]
    assert annuals[1]["netIncome"] == 200


def test_inventory_turnover_from_history():
    assert seed_build.inventory_turnover_from_history({"costOfRevenue": 400, "inventory": 100, "quarter": 4}) == 4.0
    assert seed_build.inventory_turnover_from_history({"costOfRevenue": 400, "inventory": 0, "quarter": 4}) is None
    assert seed_build.inventory_turnover_from_history({"inventory": 100, "quarter": 4}) is None


def test_history_seed_snapshots_rehydrates_monthly_revenue_history(tmp_path, monkeypatch):
    monthly_history_path = tmp_path / "monthly_revenue_history.json"
    monthly_history_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "updatedAt": "2026-06-12T00:00:00+00:00",
                "months": {
                    "3231": {
                        "2026-04": {
                            "monthlyRevenue": 207488649,
                            "monthlyRevenueYoY": 111.99,
                            "cumulativeRevenue": 696384052,
                            "cumulativeRevenueYoY": 130.59,
                        },
                        "2026-05": {
                            "monthlyRevenue": 290144591,
                            "monthlyRevenueYoY": 39.24,
                            "cumulativeRevenue": 986528642,
                            "cumulativeRevenueYoY": 106.21,
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeHistoryStore:
        def load(self):
            return {
                "quarters": {
                    "3231": {
                        "2025Q1": {"fiscalYear": 2025, "quarter": 1, "eps": 1.85, "netIncome": 5332000},
                        "2026Q1": {
                            "fiscalYear": 2026,
                            "quarter": 1,
                            "eps": 3.06,
                            "netIncome": 9630739,
                            "revenue": 567000000,
                            "grossMargin": 5.21,
                            "costOfRevenue": 537000000,
                            "inventory": 409000000,
                        },
                    }
                }
            }

    class FakeProvider:
        history_store = FakeHistoryStore()
        monthly_revenue_history = MonthlyRevenueHistoryStore(monthly_history_path)

        def list_companies(self):
            return [
                seed_build.Company(
                    stockCode="3231",
                    name="緯創",
                    market="TWSE",
                    industryName="電腦及週邊設備業",
                    isFinancial=False,
                )
            ]

    monkeypatch.setattr(seed_build, "official_provider", FakeProvider())

    snapshots = seed_build.history_seed_snapshots(seed_build.ScannerSettings())

    assert len(snapshots) == 1
    monthly = snapshots[0].monthlyRevenue
    assert monthly.month == "2026-05"
    assert monthly.monthlyRevenueYoY == 39.24
    assert monthly.previousMonthRevenueYoY == 111.99
    assert monthly.cumulativeRevenueYoY == 106.21
    assert monthly.trailingThreeMonthAverageYoY == pytest.approx(75.615)


def test_add_market_scan_summary_to_manifest():
    after_latest = seed_build.add_market_scan_summary_to_manifest(
        {"files": ["manifest.json", "market_scan_latest.json", "companies.json"]}
    )
    assert after_latest["files"] == [
        "manifest.json",
        "market_scan_latest.json",
        "market_scan_summary.json",
        "companies.json",
    ]
    appended = seed_build.add_market_scan_summary_to_manifest({"files": ["companies.json"]})
    assert appended["files"][-1] == "market_scan_summary.json"
    assert seed_build.add_market_scan_summary_to_manifest({})["files"] == ["market_scan_summary.json"]
    # Idempotent: a second pass does not duplicate the entry.
    assert after_latest["files"].count("market_scan_summary.json") == 1
    assert seed_build.add_market_scan_summary_to_manifest(after_latest)["files"].count("market_scan_summary.json") == 1


def test_write_market_generation_emits_pointer_files_and_manifest_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(seed_build, "OUT_DIR", tmp_path)

    manifest = seed_build.write_market_generation(_v2_scan_fixture(), {"files": ["market_scan_summary.json"]})
    pointer = json.loads((tmp_path / "market_scan_index.json").read_text(encoding="utf-8"))
    generation_id = pointer["generationId"]
    immutable_index = tmp_path / "market_scan" / "v2" / generation_id / "index.json"

    assert immutable_index.read_bytes() == seed_build.canonical_json_bytes(pointer)
    assert (tmp_path / "market_scan_index.json").read_bytes() == seed_build.canonical_json_bytes(pointer)
    assert manifest["marketApiSchemaVersion"] == 2
    assert manifest["marketGenerationId"] == generation_id
    assert manifest["marketIndexBytes"] == len(seed_build.canonical_json_bytes(pointer))
    assert manifest["marketPageCount"] == 1
    assert 0 < manifest["marketMaxPageBytes"] < 500 * 1024
    assert manifest["files"].count("market_scan_index.json") == 1


def test_clear_seed_output_removes_only_resolved_v2_tree(tmp_path, monkeypatch):
    output = tmp_path / "seed"
    v2_file = output / "market_scan" / "v2" / "generation" / "page.json"
    sibling = output / "market_scan" / "keep.txt"
    v2_file.parent.mkdir(parents=True)
    v2_file.write_text("stale", encoding="utf-8")
    sibling.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(seed_build, "OUT_DIR", output)
    monkeypatch.setattr(seed_build, "ANALYSIS_SHARD_DIR", output / "analysis_shards")
    monkeypatch.setattr(seed_build, "HOLDING_ANALYSIS_SHARD_DIR", output / "holding_analysis_shards")

    seed_build.clear_seed_output()

    assert not (output / "market_scan" / "v2").exists()
    assert sibling.read_text(encoding="utf-8") == "keep"


def test_clear_seed_output_checks_containment_before_deleting_any_file(tmp_path, monkeypatch):
    output = tmp_path / "seed"
    marker = output / "keep.json"
    shard = output / "analysis_shards" / "keep.json"
    marker.parent.mkdir(parents=True)
    shard.parent.mkdir(parents=True)
    marker.write_text("keep", encoding="utf-8")
    shard.write_text("keep", encoding="utf-8")
    market_v2 = output / "market_scan" / "v2"
    outside = tmp_path / "outside"
    original_resolve = Path.resolve

    def resolve_outside(self, *args, **kwargs):
        if self == market_v2:
            return original_resolve(outside, *args, **kwargs)
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(seed_build, "OUT_DIR", output)
    monkeypatch.setattr(seed_build, "ANALYSIS_SHARD_DIR", output / "analysis_shards")
    monkeypatch.setattr(seed_build, "HOLDING_ANALYSIS_SHARD_DIR", output / "holding_analysis_shards")
    monkeypatch.setattr(Path, "resolve", resolve_outside)

    with pytest.raises(RuntimeError, match="outside the seed directory"):
        seed_build.clear_seed_output()

    assert marker.read_text(encoding="utf-8") == "keep"
    assert shard.read_text(encoding="utf-8") == "keep"


def test_market_pointer_stays_unchanged_when_immutable_write_fails(tmp_path, monkeypatch):
    pointer = tmp_path / "market_scan_index.json"
    pointer.write_bytes(b"old-pointer")
    original_write_bytes = Path.write_bytes

    def fail_page_write(self, content):
        if "announced" in self.parts and self.name == "0.json":
            raise OSError("injected immutable write failure")
        return original_write_bytes(self, content)

    monkeypatch.setattr(seed_build, "OUT_DIR", tmp_path)
    monkeypatch.setattr(type(pointer), "write_bytes", fail_page_write)

    with pytest.raises(OSError, match="injected immutable"):
        seed_build.write_market_generation(_v2_scan_fixture(), {"files": []})

    assert pointer.read_bytes() == b"old-pointer"


def test_market_pointer_stays_unchanged_when_atomic_replace_fails(tmp_path, monkeypatch):
    pointer = tmp_path / "market_scan_index.json"
    pointer.write_bytes(b"old-pointer")
    monkeypatch.setattr(seed_build, "OUT_DIR", tmp_path)

    def fail_replace(source, destination):
        assert Path(source).parent == pointer.parent
        assert Path(destination) == pointer
        raise OSError("injected pointer replace failure")

    monkeypatch.setattr(seed_build.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected pointer replace"):
        seed_build.write_market_generation(_v2_scan_fixture(), {"files": []})

    assert pointer.read_bytes() == b"old-pointer"
    assert not list(tmp_path.glob(".market_scan_index.json.*.tmp"))


def test_offline_seed_copy_rebuilds_v2_generation_and_manifest_fields(tmp_path, monkeypatch):
    archive_path = tmp_path / "offline.zip"
    prefix = seed_build.OFFLINE_SEED_PREFIX
    payloads = {
        "manifest.json": {"files": ["market_scan_latest.json"], "counts": {}},
        "market_scan_latest.json": _v2_scan_fixture(),
        "analysis_by_code.json": {},
        "holding_analysis_by_code.json": {},
        "companies.json": {"items": []},
        "data_sources_status.json": {},
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, payload in payloads.items():
            archive.writestr(f"{prefix}{name}", json.dumps(payload))
        archive.writestr(f"{prefix}analysis_shards/12.json", "{}")
        archive.writestr(f"{prefix}holding_analysis_shards/12.json", "{}")

    output = tmp_path / "seed"
    monkeypatch.setattr(seed_build, "OUT_DIR", output)
    monkeypatch.setattr(seed_build, "ANALYSIS_SHARD_DIR", output / "analysis_shards")
    monkeypatch.setattr(seed_build, "HOLDING_ANALYSIS_SHARD_DIR", output / "holding_analysis_shards")
    monkeypatch.setattr(seed_build, "assert_seed_quality", lambda *args, **kwargs: None)

    manifest = seed_build.copy_offline_seed_payload(archive_path)

    assert manifest["marketApiSchemaVersion"] == 2
    assert manifest["marketGenerationId"]
    assert (output / "market_scan_index.json").is_file()
    assert (output / "market_scan" / "v2" / manifest["marketGenerationId"] / "index.json").is_file()


def test_live_seed_main_writes_v2_generation_and_manifest_fields(tmp_path, monkeypatch):
    output = tmp_path / "seed"
    no_cache = tmp_path / "missing.zip"

    class EmptyOfficialProvider:
        @staticmethod
        def list_snapshots(settings):
            return []

        @staticmethod
        def list_companies():
            return []

    monkeypatch.setenv("CLOUDFLARE_SEED_MODE", "live")
    monkeypatch.setattr(seed_build, "OUT_DIR", output)
    monkeypatch.setattr(seed_build, "ANALYSIS_SHARD_DIR", output / "analysis_shards")
    monkeypatch.setattr(seed_build, "HOLDING_ANALYSIS_SHARD_DIR", output / "holding_analysis_shards")
    monkeypatch.setattr(seed_build, "SEED_CACHE_ZIP", no_cache)
    monkeypatch.setattr(seed_build, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(seed_build, "_scan_market_payload", _v2_scan_fixture)
    monkeypatch.setattr(seed_build, "refresh_policy", lambda: {"minIntervalSeconds": 3600})
    monkeypatch.setattr(seed_build, "official_provider", EmptyOfficialProvider())
    monkeypatch.setattr(seed_build, "history_seed_snapshots", lambda settings: [])
    monkeypatch.setattr(seed_build, "assert_seed_quality", lambda *args, **kwargs: None)
    monkeypatch.setattr(seed_build, "seed_diagnostics", lambda *args, **kwargs: {"x2Missing": 0})
    monkeypatch.setattr(seed_build, "_data_sources_status_payload", lambda: {})

    seed_build.main()

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["marketApiSchemaVersion"] == 2
    assert manifest["marketGenerationId"]
    assert manifest["marketPageCount"] == 1
    assert (output / "market_scan_index.json").is_file()
    assert (output / "market_scan" / "v2" / manifest["marketGenerationId"] / "index.json").is_file()


def test_manifest_declares_all_required_official_artifacts():
    assert seed_build.OFFICIAL_MANIFEST_FILES == (
        "official_fundamentals_history.json",
        "official_history_backfill_progress.json",
        "monthly_revenue_history.json",
    )


def test_cached_seed_company_lookup_preserves_market_metadata(tmp_path):
    seed_zip = tmp_path / "seed.zip"
    with zipfile.ZipFile(seed_zip, "w") as archive:
        archive.writestr(
            "cloudflare_seed/companies.json",
            json.dumps(
                {
                    "items": [
                        {
                            "stockCode": "6488",
                            "name": "GlobalWafers",
                            "market": "TPEX",
                            "industryName": "Semiconductor",
                        }
                    ]
                }
            ),
        )

    companies = seed_build.cached_seed_company_lookup(seed_zip)

    assert companies["6488"].market == "TPEX"
    assert companies["6488"].name == "GlobalWafers"


def test_cached_seed_company_lookup_ignores_invalid_archive(tmp_path):
    invalid = tmp_path / "seed.zip"
    invalid.write_text("not a zip", encoding="utf-8")

    assert seed_build.cached_seed_company_lookup(invalid) == {}


def test_compact_scan_result_encodes_objects_and_drops_non_objects():
    encoded = seed_build.compact_scan_result(
        ObjectResult(stockCode="1234", companyName="X", status="ENTRY", summary="s", reasons=[])
    )
    assert encoded["stockCode"] == "1234"
    assert encoded["hasFullDetails"] is False
    assert seed_build.compact_scan_result(["not", "a", "dict"]) == {}


def test_seed_diagnostics_reports_counts(monkeypatch):
    monkeypatch.setattr(seed_build, "official_provider", FakeProvider())
    diagnostics = seed_build.seed_diagnostics(
        {"universeSize": 5, "entry": [1], "watch": [2, 3], "excluded": []},
        _market_companies(6, 4),
        {"a": {}},
        "cloudflare_seed_cache",
    )
    assert diagnostics["companies"] == 10
    assert diagnostics["universeSize"] == 5
    assert diagnostics["watch"] == 2
    assert diagnostics["fallbackSource"] == "cloudflare_seed_cache"
    assert diagnostics["companiesByMarket"] == {"TPEX": 4, "TWSE": 6}


def test_seed_quality_rejects_single_market_company_seed(monkeypatch):
    monkeypatch.setattr(seed_build, "official_provider", FakeProvider())
    scan_payload = {
        "universeSize": 1700,
        "entry": [{}],
        "watch": [{} for _ in range(1699)],
        "excluded": [],
    }

    with pytest.raises(RuntimeError, match="company market coverage"):
        seed_build.assert_seed_quality(
            scan_payload,
            _market_companies(twse=1700, tpex=0),
            {str(index): {} for index in range(1700)},
            fallback_source=None,
        )


def test_seed_quality_rejects_all_unrecognized_company_markets(monkeypatch):
    monkeypatch.setattr(seed_build, "official_provider", FakeProvider())
    scan_payload = {
        "universeSize": 1700,
        "entry": [{}],
        "watch": [{} for _ in range(1699)],
        "excluded": [],
    }

    with pytest.raises(RuntimeError, match="TWSE company market coverage"):
        seed_build.assert_seed_quality(
            scan_payload,
            [SimpleNamespace(market="OTHER") for _ in range(1700)],
            {str(index): {} for index in range(1700)},
            fallback_source="official_fundamentals_history",
        )


def test_seed_quality_rejects_systemic_x2_missing_data(monkeypatch):
    monkeypatch.setattr(seed_build, "official_provider", FakeProvider())
    missing_x2 = {
        "status": "INSUFFICIENT_DATA",
        "reasons": [{"code": "X2", "passed": False, "severity": "INSUFFICIENT_DATA"}],
    }
    scan_payload = {
        "universeSize": 1000,
        "entry": [],
        "watch": [missing_x2 for _ in range(1000)],
        "excluded": [],
    }

    with pytest.raises(RuntimeError, match="X2 monthly-history coverage"):
        seed_build.assert_seed_quality(
            scan_payload,
            _market_companies(),
            {str(index): {} for index in range(1000)},
            fallback_source=None,
        )


def test_seed_quality_rejects_systemic_x2_missing_from_analysis_models(monkeypatch):
    monkeypatch.setattr(seed_build, "official_provider", FakeProvider())
    missing_x2 = AnalysisResult(
        stockCode="1101",
        companyName="Test",
        status="INSUFFICIENT_DATA",
        summary="missing",
        reasons=[
            RuleResult(
                code="X2",
                title="previous month",
                passed=False,
                severity="INSUFFICIENT_DATA",
                message="missing previous month",
            )
        ],
    )
    scan_payload = {
        "universeSize": 1000,
        "entry": [],
        "watch": [missing_x2 for _ in range(1000)],
        "excluded": [],
    }

    with pytest.raises(RuntimeError, match="X2 monthly-history coverage"):
        seed_build.assert_seed_quality(
            scan_payload,
            _market_companies(),
            {str(index): {} for index in range(1000)},
            fallback_source=None,
        )


def test_seed_quality_allows_bounded_x2_missing_data(monkeypatch):
    monkeypatch.setattr(seed_build, "official_provider", FakeProvider())
    missing_x2 = {
        "status": "INSUFFICIENT_DATA",
        "reasons": [{"code": "X2", "passed": False, "severity": "INSUFFICIENT_DATA"}],
    }
    healthy = {"status": "WATCH", "reasons": [{"code": "X2", "passed": True, "severity": "INFO"}]}
    scan_payload = {
        "universeSize": 1000,
        "entry": [],
        "watch": [missing_x2 for _ in range(50)] + [healthy for _ in range(950)],
        "excluded": [],
    }

    seed_build.assert_seed_quality(
        scan_payload,
        _market_companies(),
        {str(index): {} for index in range(1000)},
        fallback_source=None,
    )
    diagnostics = seed_build.seed_diagnostics(scan_payload, _market_companies(), {}, None)
    assert diagnostics["x2Missing"] == 50
    assert diagnostics["x2MissingRatio"] == 0.05


def test_seed_quality_rejects_zero_entry_history_fallback(monkeypatch):
    monkeypatch.setattr(seed_build, "official_provider", FakeProvider())
    scan_payload = {
        "universeSize": 1000,
        "entry": [],
        "watch": [
            {"status": "WATCH", "reasons": [{"code": "X2", "passed": True, "severity": "INFO"}]} for _ in range(1000)
        ],
        "excluded": [],
    }

    with pytest.raises(RuntimeError, match="zero-entry fallback seed"):
        seed_build.assert_seed_quality(
            scan_payload,
            _market_companies(),
            {str(index): {} for index in range(1000)},
            fallback_source="official_fundamentals_history",
        )


def test_latest_financial_period_prefers_actual_cached_or_freshness_period():
    context = {
        "activeFinancialReport": {"period": "2026Q2"},
        "freshnessFinancialReport": {"period": "2026Q1"},
    }
    assert (
        seed_build.latest_financial_period(
            {"filingContext": context, "financialFreshness": {"latestCachedFinancialPeriod": "2026Q1"}}
        )
        == "2026Q1"
    )
    assert seed_build.latest_financial_period({"filingContext": context}) == "2026Q1"


def test_assert_seed_quality_rejects_small_universe_and_analysis(monkeypatch):
    monkeypatch.setattr(seed_build, "official_provider", FakeProvider())
    with pytest.raises(RuntimeError, match="market scan seed"):
        seed_build.assert_seed_quality(
            {"universeSize": 5, "entry": [], "watch": [], "excluded": []},
            _market_companies(),
            {str(index): {} for index in range(1000)},
            fallback_source=None,
        )
    with pytest.raises(RuntimeError, match="analysis seed"):
        seed_build.assert_seed_quality(
            {"universeSize": 1000, "entry": [], "watch": [{} for _ in range(1000)], "excluded": []},
            _market_companies(),
            {"only": {}},
            fallback_source=None,
        )


def test_assert_seed_quality_rejects_stale_financial_freshness(monkeypatch):
    monkeypatch.setattr(seed_build, "official_provider", FakeProvider())

    with pytest.raises(RuntimeError, match="financial freshness"):
        seed_build.assert_seed_quality(
            {
                "universeSize": 1000,
                "entry": [],
                "watch": [{} for _ in range(1000)],
                "excluded": [],
                "financialFreshness": {
                    "status": "stale",
                    "blocksDeployment": True,
                    "expectedFinancialPeriod": "2026Q1",
                    "latestCachedFinancialPeriod": "2025Q4",
                },
            },
            _market_companies(),
            {str(index): {} for index in range(1000)},
            fallback_source=None,
        )

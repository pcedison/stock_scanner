from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

import scripts.validate_cloudflare_seed_inputs as validator_module
from backend.services.market_query import MAX_INDEX_BYTES, MAX_PAGE_BYTES, build_market_generation, canonical_json_bytes
from scripts.validate_cloudflare_seed_inputs import (
    _load_json_from_zip,
    failed_company_summary,
    main,
    render_seed_summary,
    validate_seed_freshness,
    validate_seed_zip,
)

TEST_SEED_COMPANIES = 1700
_SUMMARY_RESULT_KEYS = ("stockCode", "companyName", "status", "summary")
_SUMMARY_REASON_KEYS = ("code", "title", "passed", "severity", "message")
_SUMMARY_REASON_CODES = {"E4", "OFFICIAL_Q", "OFFICIAL_VALUATION", "X1", "X2", "X3", "X4", "X5"}


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


def _compact_generation_scan(scan: dict[str, object]) -> dict[str, object]:
    compact = dict(scan)
    for category in ("entry", "watch", "excluded", "results"):
        items = compact.get(category)
        if not isinstance(items, list):
            continue
        compact_items = []
        for item in items:
            assert isinstance(item, dict)
            result = {key: item.get(key) for key in _SUMMARY_RESULT_KEYS if key in item}
            reasons = item.get("reasons")
            if isinstance(reasons, list):
                result["reasons"] = [
                    {key: reason.get(key) for key in _SUMMARY_REASON_KEYS if key in reason}
                    for reason in reasons
                    if isinstance(reason, dict) and str(reason.get("code") or "") in _SUMMARY_REASON_CODES
                ]
            result["detailsAvailable"] = True
            result["hasFullDetails"] = False
            compact_items.append(result)
        compact[category] = compact_items
    compact["detailMode"] = "summary"
    return compact


def _write_seed_zip(
    path: Path,
    companies: int = 1000,
    rows_per_company: int = 5,
    seed_companies: int = TEST_SEED_COMPANIES,
) -> None:
    members = _seed_members(
        companies=companies,
        rows=rows_per_company,
        seed_company_count=seed_companies,
    )
    with zipfile.ZipFile(path, "w") as archive:
        for entry, content in members.items():
            archive.writestr(entry, content)


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
    members = _seed_members()
    members["official_fundamentals_history.json"] = json.dumps({"quarters": {}})
    archive_path = _zip_from(tmp_path, members)

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
    entry_count: int = 10,
    excluded_count: int = 10,
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
    selected_manifest = json.loads(json.dumps(default_manifest if manifest is None else manifest))
    scan_counts = {
        "entry": entry_count,
        "watch": companies - entry_count - excluded_count,
        "excluded": excluded_count,
    }
    if isinstance(selected_manifest, dict) and isinstance(selected_manifest.get("counts"), dict):
        candidate_counts = {
            category: selected_manifest["counts"].get(category) for category in ("entry", "watch", "excluded")
        }
        if all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in candidate_counts.values()
        ):
            scan_counts = candidate_counts
    def rows(category: str, count: int) -> list[dict[str, object]]:
        prefix = {"entry": "E", "watch": "W", "excluded": "X"}[category]
        status = {"entry": "ENTRY", "watch": "INSUFFICIENT_DATA", "excluded": "EXCLUDED"}[category]
        reasons = (
            [{"code": "OFFICIAL_Q", "severity": "INFO", "message": "2026Q1 official financials"}]
            if category != "watch"
            else [{"code": "OFFICIAL_Q", "severity": "INSUFFICIENT_DATA", "message": "2026Q2 not released"}]
        )
        return [
            {
                "stockCode": f"{prefix}{index:06d}",
                "companyName": f"{category}-{index}",
                "status": status,
                "reasons": reasons,
            }
            for index in range(count)
        ]

    market_scan = {
        "generatedAt": "2026-05-17T00:00:00+00:00",
        "universeSize": sum(scan_counts.values()),
        "filingContext": {
            "freshnessFinancialReport": {"period": "2026Q1"},
            "activeFinancialReport": {"period": "2026Q2"},
            "monthlyRevenuePeriod": "2026-06",
        },
        "financialFreshness": {"latestCachedFinancialPeriod": "2026Q1"},
        "entry": rows("entry", scan_counts["entry"]),
        "watch": rows("watch", scan_counts["watch"]),
        "excluded": rows("excluded", scan_counts["excluded"]),
    }
    cache_status_inputs = {
        "generatedAt": market_scan["generatedAt"],
        "latestRevenuePeriod": market_scan["filingContext"]["monthlyRevenuePeriod"],
        "latestFinancialPeriod": market_scan["financialFreshness"]["latestCachedFinancialPeriod"],
    }
    generation = build_market_generation(
        _compact_generation_scan(market_scan),
        cache_status_inputs=cache_status_inputs,
    )
    if isinstance(selected_manifest, dict):
        page_sizes = [
            len(content)
            for key, content in generation.files.items()
            if not key.endswith("/index.json")
        ]
        selected_manifest.update(
            {
                "marketApiSchemaVersion": 2,
                "marketGenerationId": generation.generation_id,
                "marketIndexBytes": len(canonical_json_bytes(generation.index)),
                "marketPageCount": len(page_sizes),
                "marketMaxPageBytes": max(page_sizes, default=0),
            }
        )

    members: dict[str, str | bytes] = {
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
        "cloudflare_seed/market_scan_latest.json": json.dumps(market_scan),
        "cloudflare_seed/market_scan_index.json": canonical_json_bytes(generation.index),
        "cloudflare_seed/analysis_by_code.json": "{}",
        "cloudflare_seed/holding_analysis_by_code.json": "{}",
        "cloudflare_seed/analysis_shards/10.json": "{}",
        "cloudflare_seed/holding_analysis_shards/10.json": "{}",
    }
    members.update(
        {
            f"cloudflare_seed/{object_key.removeprefix('public/')}": content
            for object_key, content in generation.files.items()
        }
    )
    return members


def _zip_from(tmp_path: Path, members: dict[str, str | bytes], name: str = "seed.zip") -> Path:
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


def _v2_pointer(members: dict[str, str | bytes]) -> dict[str, object]:
    raw = members["cloudflare_seed/market_scan_index.json"]
    return json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))


def _sync_v2_index(members: dict[str, str | bytes], pointer: dict[str, object]) -> None:
    raw = canonical_json_bytes(pointer)
    generation_id = pointer["generationId"]
    members["cloudflare_seed/market_scan_index.json"] = raw
    members[f"cloudflare_seed/market_scan/v2/{generation_id}/index.json"] = raw


def _first_v2_ref(pointer: dict[str, object], disclosure: str, category: str, index: int = 0) -> dict[str, object]:
    return pointer["disclosures"][disclosure][category]["pages"][index]


def _replace_v2_page(
    members: dict[str, str | bytes],
    pointer: dict[str, object],
    reference: dict[str, object],
    page: dict[str, object],
) -> None:
    raw = canonical_json_bytes(page)
    reference["bytes"] = len(raw)
    reference["sha256"] = hashlib.sha256(raw).hexdigest()
    members[f"cloudflare_seed/{str(reference['key']).removeprefix('public/')}"] = raw
    _sync_v2_index(members, pointer)


def test_validate_seed_zip_reports_verified_v2_generation_summary(tmp_path):
    summary = validate_seed_zip(_zip_from(tmp_path, _seed_members()))

    assert len(summary["marketGenerationId"]) == 24
    assert summary["marketPageCount"] == 12
    assert 0 < summary["marketMaxPageBytes"] < MAX_PAGE_BYTES


def test_validate_seed_zip_rejects_duplicate_zip_members(tmp_path):
    archive_path = _zip_from(tmp_path, _seed_members())
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(archive_path, "a") as archive:
            archive.writestr("cloudflare_seed/market_scan_index.json", "{}")

    with pytest.raises(ValueError, match="duplicate ZIP member"):
        validate_seed_zip(archive_path)


def test_validate_seed_zip_requires_pointer_and_immutable_index_raw_bytes_to_match(tmp_path):
    members = _seed_members()
    pointer = _v2_pointer(members)
    immutable_name = f"cloudflare_seed/market_scan/v2/{pointer['generationId']}/index.json"
    members[immutable_name] = bytes(members[immutable_name]) + b"\n"

    with pytest.raises(ValueError, match="pointer bytes do not match immutable generation index"):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_zip_rejects_missing_and_orphan_generation_files(tmp_path):
    members = _seed_members()
    pointer = _v2_pointer(members)
    missing_ref = _first_v2_ref(pointer, "pending", "watch")
    del members[f"cloudflare_seed/{str(missing_ref['key']).removeprefix('public/')}"]
    with pytest.raises(ValueError, match="v2 generation files do not match pointer references"):
        validate_seed_zip(_zip_from(tmp_path, members))

    members = _seed_members()
    members[f"cloudflare_seed/market_scan/v2/{'f' * 24}/index.json"] = "{}"
    with pytest.raises(ValueError, match="v2 generation files do not match pointer references"):
        validate_seed_zip(_zip_from(tmp_path, members, name="orphan.zip"))


def test_validate_seed_zip_rejects_unsafe_and_duplicate_page_references(tmp_path):
    members = _seed_members()
    pointer = _v2_pointer(members)
    reference = _first_v2_ref(pointer, "announced", "entry")
    reference["key"] = "public/market_scan/v2/../../secret.json"
    _sync_v2_index(members, pointer)
    with pytest.raises(ValueError, match="unsafe market page path"):
        validate_seed_zip(_zip_from(tmp_path, members))

    members = _seed_members()
    pointer = _v2_pointer(members)
    references = pointer["disclosures"]["pending"]["watch"]["pages"]
    references[1]["cursor"] = references[0]["cursor"]
    _sync_v2_index(members, pointer)
    with pytest.raises(ValueError, match="page references must be contiguous"):
        validate_seed_zip(_zip_from(tmp_path, members, name="duplicate-cursor.zip"))


def test_validate_seed_zip_checks_raw_page_hash_and_size(tmp_path):
    members = _seed_members()
    pointer = _v2_pointer(members)
    reference = _first_v2_ref(pointer, "pending", "watch")
    reference["sha256"] = "0" * 64
    _sync_v2_index(members, pointer)
    with pytest.raises(ValueError, match="page SHA-256"):
        validate_seed_zip(_zip_from(tmp_path, members))

    members = _seed_members()
    pointer = _v2_pointer(members)
    reference = _first_v2_ref(pointer, "pending", "watch")
    raw = b'{' + b'"padding":"' + (b"x" * MAX_PAGE_BYTES) + b'"}'
    reference["bytes"] = len(raw)
    reference["sha256"] = hashlib.sha256(raw).hexdigest()
    members[f"cloudflare_seed/{str(reference['key']).removeprefix('public/')}"] = raw
    _sync_v2_index(members, pointer)
    with pytest.raises(ValueError, match="page size"):
        validate_seed_zip(_zip_from(tmp_path, members, name="large-page.zip"))


def test_validate_seed_zip_rejects_index_size_overflow(tmp_path):
    members = _seed_members()
    pointer = _v2_pointer(members)
    pointer["padding"] = "x" * MAX_INDEX_BYTES
    _sync_v2_index(members, pointer)

    with pytest.raises(ValueError, match="index size"):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_zip_checks_page_next_cursor_and_aggregate_counts(tmp_path):
    members = _seed_members()
    pointer = _v2_pointer(members)
    reference = _first_v2_ref(pointer, "pending", "watch")
    page_name = f"cloudflare_seed/{str(reference['key']).removeprefix('public/')}"
    page = json.loads(bytes(members[page_name]))
    page["nextCursor"] = 999
    _replace_v2_page(members, pointer, reference, page)
    with pytest.raises(ValueError, match="nextCursor"):
        validate_seed_zip(_zip_from(tmp_path, members))

    members = _seed_members()
    pointer = _v2_pointer(members)
    pointer["counts"]["categories"]["watch"] -= 1
    _sync_v2_index(members, pointer)
    with pytest.raises(ValueError, match="aggregate counts"):
        validate_seed_zip(_zip_from(tmp_path, members, name="count.zip"))


def test_validate_seed_zip_requires_v1_v2_identity_and_freshness_classification_parity(tmp_path):
    members = _seed_members()
    pointer = _v2_pointer(members)
    reference = _first_v2_ref(pointer, "pending", "watch")
    page_name = f"cloudflare_seed/{str(reference['key']).removeprefix('public/')}"
    page = json.loads(bytes(members[page_name]))
    page["items"][1]["stockCode"] = page["items"][0]["stockCode"]
    _replace_v2_page(members, pointer, reference, page)

    with pytest.raises(ValueError, match="duplicate market identity|identity parity"):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_zip_cross_checks_manifest_v2_raw_metrics(tmp_path):
    members = _seed_members()
    manifest = json.loads(members["cloudflare_seed/manifest.json"])
    manifest["marketMaxPageBytes"] += 1
    members["cloudflare_seed/manifest.json"] = json.dumps(manifest)

    with pytest.raises(ValueError, match="manifest marketMaxPageBytes"):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_zip_rejects_wrong_page_generation_and_cursor_gap(tmp_path):
    members = _seed_members()
    pointer = _v2_pointer(members)
    reference = _first_v2_ref(pointer, "pending", "watch")
    page_name = f"cloudflare_seed/{str(reference['key']).removeprefix('public/')}"
    page = json.loads(bytes(members[page_name]))
    page["generationId"] = "f" * 24
    _replace_v2_page(members, pointer, reference, page)
    with pytest.raises(ValueError, match="page identity fields"):
        validate_seed_zip(_zip_from(tmp_path, members))

    members = _seed_members()
    pointer = _v2_pointer(members)
    references = pointer["disclosures"]["pending"]["watch"]["pages"]
    references[1]["cursor"] = 101
    references[1]["key"] = str(references[1]["key"]).replace("/100.json", "/101.json")
    _sync_v2_index(members, pointer)
    with pytest.raises(ValueError, match="page references must be contiguous"):
        validate_seed_zip(_zip_from(tmp_path, members, name="cursor-gap.zip"))


@pytest.mark.parametrize("extra_kind", ["disclosure", "category", "reference"])
def test_validate_seed_zip_rejects_extra_market_index_structure(tmp_path, extra_kind):
    members = _seed_members()
    pointer = _v2_pointer(members)
    if extra_kind == "disclosure":
        pointer["disclosures"]["archived"] = {}
    elif extra_kind == "category":
        pointer["disclosures"]["announced"]["bonus"] = {"count": 0, "pages": []}
    else:
        pointer["disclosures"]["announced"]["entry"]["pages"][0]["unexpected"] = True
    _sync_v2_index(members, pointer)

    with pytest.raises(ValueError, match="unexpected market index fields"):
        validate_seed_zip(_zip_from(tmp_path, members))


@pytest.mark.parametrize("oversized_member", ["pointer", "immutable-index", "page"])
def test_validate_seed_zip_rejects_oversized_members_before_read(tmp_path, monkeypatch, oversized_member):
    members = _seed_members()
    pointer = _v2_pointer(members)
    generation_id = pointer["generationId"]
    if oversized_member == "pointer":
        target = "cloudflare_seed/market_scan_index.json"
        members[target] = b"x" * MAX_INDEX_BYTES
        expected_error = "market index size"
    elif oversized_member == "immutable-index":
        target = f"cloudflare_seed/market_scan/v2/{generation_id}/index.json"
        members[target] = b"x" * MAX_INDEX_BYTES
        expected_error = "market index size"
    else:
        reference = _first_v2_ref(pointer, "pending", "watch")
        target = f"cloudflare_seed/{str(reference['key']).removeprefix('public/')}"
        members[target] = b"x" * MAX_PAGE_BYTES
        expected_error = "market page size"
    archive_path = _zip_from(tmp_path, members)
    real_read = zipfile.ZipFile.read

    def explode_if_oversized_is_read(archive, name, *args, **kwargs):
        member_name = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if member_name == target:
            raise AssertionError(f"oversized member was read: {target}")
        return real_read(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", explode_if_oversized_is_read)

    with pytest.raises(ValueError, match=expected_error):
        validate_seed_zip(archive_path)


def test_validate_seed_zip_rejects_consistently_rewritten_non_content_generation_id(tmp_path):
    members = _seed_members()
    pointer = _v2_pointer(members)
    old_id = pointer["generationId"]
    new_id = "f" * 24
    pointer["generationId"] = new_id
    rewritten: dict[str, str | bytes] = {}
    for disclosure in ("announced", "pending"):
        for category in ("entry", "watch", "excluded"):
            for reference in pointer["disclosures"][disclosure][category]["pages"]:
                old_key = str(reference["key"])
                old_name = f"cloudflare_seed/{old_key.removeprefix('public/')}"
                page = json.loads(bytes(members.pop(old_name)))
                page["generationId"] = new_id
                reference["key"] = old_key.replace(old_id, new_id)
                raw = canonical_json_bytes(page)
                reference["bytes"] = len(raw)
                reference["sha256"] = hashlib.sha256(raw).hexdigest()
                rewritten[f"cloudflare_seed/{str(reference['key']).removeprefix('public/')}"] = raw
    members.pop(f"cloudflare_seed/market_scan/v2/{old_id}/index.json")
    members.update(rewritten)
    _sync_v2_index(members, pointer)
    manifest = json.loads(members["cloudflare_seed/manifest.json"])
    page_sizes = [len(content) for name, content in rewritten.items() if name.endswith(".json")]
    manifest.update(
        {
            "marketGenerationId": new_id,
            "marketIndexBytes": len(canonical_json_bytes(pointer)),
            "marketPageCount": len(rewritten),
            "marketMaxPageBytes": max(page_sizes),
        }
    )
    members["cloudflare_seed/manifest.json"] = json.dumps(manifest)

    with pytest.raises(ValueError, match="content-derived generation ID"):
        validate_seed_zip(_zip_from(tmp_path, members))


def test_validate_seed_zip_rejects_boolean_top_level_count_before_reading_pages(tmp_path, monkeypatch):
    members = _seed_members(entry_count=1)
    pointer = _v2_pointer(members)
    pointer["counts"]["categories"]["entry"] = True
    _sync_v2_index(members, pointer)
    archive_path = _zip_from(tmp_path, members)
    real_read = zipfile.ZipFile.read

    def explode_if_page_is_read(archive, name, *args, **kwargs):
        member_name = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if "/market_scan/v2/" in member_name and not member_name.endswith("/index.json"):
            raise AssertionError(f"page was read before count validation: {member_name}")
        return real_read(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", explode_if_page_is_read)

    with pytest.raises(ValueError, match="non-negative integer"):
        validate_seed_zip(archive_path)


def test_validate_seed_zip_rejects_same_generation_unreferenced_page_and_unique_identity_gap(tmp_path):
    members = _seed_members()
    pointer = _v2_pointer(members)
    generation_id = pointer["generationId"]
    members[f"cloudflare_seed/market_scan/v2/{generation_id}/pending/watch/9999.json"] = "{}"
    with pytest.raises(ValueError, match="generation files do not match pointer references"):
        validate_seed_zip(_zip_from(tmp_path, members))

    members = _seed_members()
    pointer = _v2_pointer(members)
    reference = _first_v2_ref(pointer, "pending", "watch")
    page_name = f"cloudflare_seed/{str(reference['key']).removeprefix('public/')}"
    page = json.loads(bytes(members[page_name]))
    page["items"][0]["stockCode"] = "UNIQUE-NOT-IN-V1"
    _replace_v2_page(members, pointer, reference, page)
    with pytest.raises(ValueError, match="identity parity"):
        validate_seed_zip(_zip_from(tmp_path, members, name="identity-gap.zip"))


@pytest.mark.parametrize("unsafe_name", ["../escape.json", "/absolute.json", r"cloudflare_seed\evil.json"])
def test_validate_seed_zip_rejects_unsafe_member_names(tmp_path, unsafe_name):
    if "\\" in unsafe_name:
        archive_path = _zip_from(tmp_path, _seed_members())
        normalized_name = unsafe_name.replace("\\", "/")
        with zipfile.ZipFile(archive_path, "a") as archive:
            archive.writestr(normalized_name, "{}")
        archive_path.write_bytes(
            archive_path.read_bytes().replace(normalized_name.encode(), unsafe_name.encode())
        )
    else:
        members = _seed_members()
        members[unsafe_name] = "{}"
        archive_path = _zip_from(tmp_path, members)

    with pytest.raises(ValueError, match="unsafe ZIP member name"):
        validate_seed_zip(archive_path)


def test_validate_seed_zip_rejects_unix_symlink_member(tmp_path):
    archive_path = _zip_from(tmp_path, _seed_members())
    info = zipfile.ZipInfo("cloudflare_seed/symlink.json")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "a") as archive:
        archive.writestr(info, "../../outside.json")

    with pytest.raises(ValueError, match="symbolic link"):
        validate_seed_zip(archive_path)


def test_validate_seed_zip_rejects_cache_status_not_derived_from_legacy_scan(tmp_path):
    members = _seed_members()
    market_scan = json.loads(members["cloudflare_seed/market_scan_latest.json"])
    cache_status_inputs = {
        "generatedAt": market_scan["generatedAt"],
        "latestRevenuePeriod": market_scan["filingContext"]["monthlyRevenuePeriod"],
        "latestFinancialPeriod": "2099Q4",
    }
    generation = build_market_generation(
        _compact_generation_scan(market_scan),
        cache_status_inputs=cache_status_inputs,
    )
    pointer = generation.index
    members = {
        name: content
        for name, content in members.items()
        if not name.startswith("cloudflare_seed/market_scan/v2/")
    }
    members["cloudflare_seed/market_scan_index.json"] = canonical_json_bytes(pointer)
    members.update(
        {
            f"cloudflare_seed/{key.removeprefix('public/')}": content
            for key, content in generation.files.items()
        }
    )
    manifest = json.loads(members["cloudflare_seed/manifest.json"])
    page_sizes = [len(content) for key, content in generation.files.items() if not key.endswith("/index.json")]
    manifest.update(
        marketGenerationId=generation.generation_id,
        marketIndexBytes=len(canonical_json_bytes(pointer)),
        marketPageCount=len(page_sizes),
        marketMaxPageBytes=max(page_sizes),
    )
    members["cloudflare_seed/manifest.json"] = json.dumps(manifest)

    with pytest.raises(ValueError, match="cacheStatusInputs"):
        validate_seed_zip(_zip_from(tmp_path, members))


@pytest.mark.parametrize("mutation", ["pointer-field", "page-content"])
def test_validate_seed_zip_rejects_noncanonical_generation_bytes(tmp_path, mutation):
    members = _seed_members()
    pointer = _v2_pointer(members)
    if mutation == "pointer-field":
        pointer["unexpected"] = True
        _sync_v2_index(members, pointer)
        manifest = json.loads(members["cloudflare_seed/manifest.json"])
        manifest["marketIndexBytes"] = len(canonical_json_bytes(pointer))
        members["cloudflare_seed/manifest.json"] = json.dumps(manifest)
    else:
        reference = _first_v2_ref(pointer, "pending", "watch")
        page_name = f"cloudflare_seed/{str(reference['key']).removeprefix('public/')}"
        page = json.loads(bytes(members[page_name]))
        page["items"][0]["companyName"] = "tampered"
        _replace_v2_page(members, pointer, reference, page)

    with pytest.raises(ValueError, match="canonical generation bytes"):
        validate_seed_zip(_zip_from(tmp_path, members, name=f"{mutation}.zip"))


def test_validate_seed_zip_rejects_unexpected_safe_member_without_reading_it(tmp_path, monkeypatch):
    members = _seed_members()
    target = "cloudflare_seed/extra.json"
    members[target] = "{}"
    archive_path = _zip_from(tmp_path, members)
    real_read = zipfile.ZipFile.read

    def explode_if_extra_is_read(archive, name, *args, **kwargs):
        member_name = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if member_name == target:
            raise AssertionError("unexpected member was decompressed")
        return real_read(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", explode_if_extra_is_read)

    with pytest.raises(ValueError, match="unexpected ZIP member"):
        validate_seed_zip(archive_path)


def test_validate_seed_zip_rejects_archive_uncompressed_budget_before_json_reads(tmp_path, monkeypatch):
    archive_path = _zip_from(tmp_path, _seed_members())
    monkeypatch.setattr(validator_module, "MAX_ZIP_UNCOMPRESSED_BYTES", 1)
    monkeypatch.setattr(
        zipfile.ZipFile,
        "read",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("archive member was decompressed")),
    )

    with pytest.raises(ValueError, match="uncompressed size"):
        validate_seed_zip(archive_path)

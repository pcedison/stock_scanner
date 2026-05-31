import pytest

import scripts.build_cloudflare_seed as seed_build


class ObjectResult:
    def __init__(self, **values):
        self.__dict__.update(values)


class FakeProvider:
    def status(self, refresh=False):
        return {"companies": 0, "monthlySnapshots": 1000, "lastError": "profile endpoint returned no companies"}


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
                    }
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
    assert [reason["code"] for reason in compact["entry"][0]["reasons"]] == ["E4"]
    assert "evidence" not in compact["entry"][0]["reasons"][0]


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
        [None] * 10,
        {"a": {}},
        "cloudflare_seed_cache",
    )
    assert diagnostics["companies"] == 10
    assert diagnostics["universeSize"] == 5
    assert diagnostics["watch"] == 2
    assert diagnostics["fallbackSource"] == "cloudflare_seed_cache"


def test_assert_seed_quality_rejects_small_universe_and_analysis(monkeypatch):
    monkeypatch.setattr(seed_build, "official_provider", FakeProvider())
    with pytest.raises(RuntimeError, match="market scan seed"):
        seed_build.assert_seed_quality(
            {"universeSize": 5, "entry": [], "watch": [], "excluded": []},
            [None] * 1000,
            {str(index): {} for index in range(1000)},
            fallback_source=None,
        )
    with pytest.raises(RuntimeError, match="analysis seed"):
        seed_build.assert_seed_quality(
            {"universeSize": 1000, "entry": [], "watch": [{} for _ in range(1000)], "excluded": []},
            [None] * 1000,
            {"only": {}},
            fallback_source=None,
        )

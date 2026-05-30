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

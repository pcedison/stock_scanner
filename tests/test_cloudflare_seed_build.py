from scripts.build_cloudflare_seed import (
    compact_market_scan_payload,
    rebuild_scan_from_analysis,
    scan_payload_needs_rebuild,
)


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

    compact = compact_market_scan_payload(payload)

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

    assert scan_payload_needs_rebuild(lazy_scan) is True
    rebuilt = rebuild_scan_from_analysis(lazy_scan, analysis_results)
    compact = compact_market_scan_payload(rebuilt)

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

    assert scan_payload_needs_rebuild(scan) is False
